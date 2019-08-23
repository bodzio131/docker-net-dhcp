import itertools
import ipaddress
from os import path
import logging
import atexit

import pyroute2
import docker
from flask import request, jsonify

from . import NetDhcpError, app

OPTS_KEY = 'com.docker.network.generic' 
BRIDGE_OPT = 'devplayer0.net-dhcp.bridge'

logger = logging.getLogger('gunicorn.error')

ipdb = pyroute2.IPDB()
@atexit.register
def close_ipdb():
    ipdb.release()

client = docker.from_env()
@atexit.register
def close_docker():
    client.close()

def veth_pair(e):
    return f'dh-{e[:12]}', f'{e[:12]}-dh'

def iface_addrs(iface):
    return list(map(ipaddress.ip_interface, iface.ipaddr))
def iface_nets(iface):
    return list(map(lambda n: n.network, map(ipaddress.ip_interface, iface.ipaddr)))

def get_bridges():
    reserved_nets = set(map(ipaddress.ip_network, map(lambda c: c['Subnet'], \
        itertools.chain.from_iterable(map(lambda i: i['Config'], filter(lambda i: i['Driver'] != 'net-dhcp', \
            map(lambda n: n.attrs['IPAM'], client.networks.list())))))))

    return dict(map(lambda i: (i.ifname, i), filter(lambda i: i.kind == 'bridge' and not \
        set(iface_nets(i)).intersection(reserved_nets), map(lambda i: ipdb.interfaces[i], \
            filter(lambda k: isinstance(k, str), ipdb.interfaces.keys())))))

def net_bridge(n):
    return ipdb.interfaces[client.networks.get(n).attrs['Options'][BRIDGE_OPT]]

@app.route('/NetworkDriver.GetCapabilities', methods=['POST'])
def net_get_capabilities():
    return jsonify({
        'Scope': 'local',
        'ConnectivityScope': 'global'
    })

@app.route('/NetworkDriver.CreateNetwork', methods=['POST'])
def create_net():
    req = request.get_json(force=True)
    if BRIDGE_OPT not in req['Options'][OPTS_KEY]:
        return jsonify({'Err': 'No bridge provided'}), 400

    desired = req['Options'][OPTS_KEY][BRIDGE_OPT]
    bridges = get_bridges()
    if desired not in bridges:
        return jsonify({'Err': f'Bridge "{desired}" not found (or the specified bridge is already used by Docker)'}), 400

    if request.json['IPv6Data']:
        return jsonify({'Err': 'IPv6 is currently unsupported'}), 400

    logger.info(f'Creating network "{req["NetworkID"]}" (using bridge "{desired}")')
    return jsonify({})

@app.route('/NetworkDriver.DeleteNetwork', methods=['POST'])
def delete_net():
    return jsonify({})

@app.route('/NetworkDriver.CreateEndpoint', methods=['POST'])
def create_endpoint():
    req = request.get_json(force=True)
    req_iface = req['Interface']

    bridge = net_bridge(req['NetworkID'])
    bridge_addrs = iface_addrs(bridge)

    if_host, if_container = veth_pair(req['EndpointID'])
    logger.info(f'creating veth pair {if_host} <=> {if_container}')
    if_host = (ipdb.create(ifname=if_host, kind='veth', peer=if_container)
                .up()
                .commit())

    if_container = (ipdb.interfaces[if_container]
                    .up()
                    .commit())
    res_iface = {
        'MacAddress': '',
        'Address': '',
        'AddressIPv6': ''
    }

    try:
        if 'MacAddress' in req_iface and req_iface['MacAddress']:
            if_container.address = req_iface['MacAddress']
            if_container.commit()
        else:
            res_iface['MacAddress'] = if_container.address

        def try_addr(type_):
            k = 'AddressIPv6' if type_ == 'v6' else 'Address'
            if k in req_iface and req_iface[k]:
                a = ipaddress.ip_address(req_iface[k])
                net = None
                for addr in bridge_addrs:
                    if a == addr.ip:
                        raise NetDhcpError(400, f'Address {a} is already in use on bridge {bridge.ifname}')
                    if a in addr.network:
                        net = addr.network
                if not net:
                    raise NetDhcpError(400, f'No suitable network found for {type_} address {a} on bridge {bridge.ifname}')

                to_add = f'{a}/{net.prefixlen}'
                logger.info(f'Adding address {a}/{net.prefixlen} to {if_container.ifname}')
                (if_container
                    .add_ip(to_add)
                    .commit())
            elif type == 'v4':
                raise NetDhcpError(400, f'DHCP{type_} is currently unsupported')
        try_addr('v4')
        try_addr('v6')

        (bridge
            .add_port(if_host)
            .commit())

        res = jsonify({
            'Interface': res_iface
        })
    except NetDhcpError as e:
        (if_host
            .remove()
            .commit())
        logger.error(e)
        res = jsonify({'Err': str(e)}), e.status
    except Exception as e:
        (if_host
            .remove()
            .commit())
        res = jsonify({'Err': str(e)}), 500
    finally:
        return res

@app.route('/NetworkDriver.EndpointOperInfo', methods=['POST'])
def endpoint_info():
    req = request.get_json(force=True)

    bridge = net_bridge(req['NetworkID'])
    if_host, _if_container = veth_pair(req['EndpointID'])
    if_host = ipdb.interfaces[if_host]

    return jsonify({
        'bridge': bridge,
        'if_host': {
            'name': if_host.ifname,
            'mac': if_host.address
        }
    })

@app.route('/NetworkDriver.DeleteEndpoint', methods=['POST'])
def delete_endpoint():
    req = request.get_json(force=True)

    bridge = net_bridge(req['NetworkID'])
    if_host, _if_container = veth_pair(req['EndpointID'])
    if_host = ipdb.interfaces[if_host]

    bridge.del_port(if_host.ifname)
    (if_host
        .remove()
        .commit())

    return jsonify({})
