# -*- coding: utf-8 -*-

# pylint: disable=unused-variable

import socket
import os
import httpx

from time   import time
from io     import BytesIO
from quart  import Response, render_template, request, jsonify, send_from_directory, send_file, abort

from ifaces import Ifaces4
from histodraw import render

def add_api_views(app, mp):
    @app.route("/")
    async def index():
        return await render_template(
            "index.html",
            Hostname=socket.gethostname(),
            HaveProm=("true" if "MESHPING_PROMETHEUS_URL" in os.environ else "false"),
        )

    @app.route("/metrics")
    async def metrics():
        respdata = ['\n'.join([
            '# HELP meshping_sent Sent pings',
            '# TYPE meshping_sent counter',
            '# HELP meshping_recv Received pongs',
            '# TYPE meshping_recv counter',
            '# HELP meshping_lost Lost pings (actual counter, not just sent - recv)',
            '# TYPE meshping_lost counter',
            '# HELP meshping_max max ping',
            '# TYPE meshping_max gauge',
            '# HELP meshping_min min ping',
            '# TYPE meshping_min gauge',
            '# HELP meshping_pings Pings bucketed by response time',
            '# TYPE meshping_pings histogram',
        ])]

        for _, name, addr in mp.iter_targets():
            target = mp.get_target_info(addr, name)
            respdata.append('\n'.join([
                'meshping_sent{name="%(name)s",target="%(addr)s"} %(sent)d',

                'meshping_recv{name="%(name)s",target="%(addr)s"} %(recv)d',

                'meshping_lost{name="%(name)s",target="%(addr)s"} %(lost)d',
            ]) % target)

            if target["recv"]:
                target = dict(target, avg=(target["sum"] / target["recv"]))
                respdata.append('\n'.join([
                    'meshping_max{name="%(name)s",target="%(addr)s"} %(max).2f',

                    'meshping_min{name="%(name)s",target="%(addr)s"} %(min).2f',
                ]) % target)

            respdata.append('\n'.join([
                'meshping_pings_sum{name="%(name)s",target="%(addr)s"} %(sum)f',
                'meshping_pings_count{name="%(name)s",target="%(addr)s"} %(recv)d',
            ]) % target)

            histogram = mp.get_target_histogram(addr)
            buckets = sorted(histogram.keys(), key=float)
            count = 0
            for bucket in buckets:
                nextping = 2 ** ((bucket + 1) / 10.) - 0.01
                count += histogram[bucket]
                respdata.append(
                    'meshping_pings_bucket{name="%(name)s",target="%(addr)s",le="%(le).2f"} %(count)d' % dict(
                        addr  = addr,
                        count = count,
                        le    = nextping,
                        name  = target['name'],
                    )
                )

        return Response('\n'.join(respdata) + '\n', mimetype="text/plain")

    @app.route("/peer", methods=["POST"])
    async def peer():
        # Allows peers to POST a json structure such as this:
        # {
        #    "targets": [
        #       { "name": "raspi",  "addr": "192.168.0.123", "local": true  },
        #       { "name": "google", "addr": "8.8.8.8",       "local": false }
        #    ]
        # }
        # The non-local targets will then be added to our target list
        # and stats will be returned for these targets (if known).
        # Local targets will only be added if they are also local to us.

        request_json = await request.get_json()

        if request_json is None:
            return "Please send content-type:application/json", 400

        if not isinstance(request_json.get("targets"), list):
            return "need targets as a list", 400

        stats = []
        if4   = Ifaces4()

        for target in request_json["targets"]:
            if not isinstance(target, dict):
                return "targets must be dicts", 400
            if  "name" not in target  or not target["name"].strip() or \
                "addr" not in target  or not target["addr"].strip() or \
                "local" not in target or not isinstance(target["local"], bool):
                return "required field missing in target", 400

            target["name"] = target["name"].strip()
            target["addr"] = target["addr"].strip()

            if if4.is_interface(target["addr"]):
                # no need to ping my own interfaces, ignore
                continue

            if target["local"] and not if4.is_local(target["addr"]):
                continue

            target_str = "%(name)s@%(addr)s" % target
            mp.add_target(target_str)
            stats.append(mp.get_target_info(target["addr"], target["name"]))

        return jsonify(success=True, targets=stats)

    @app.route('/ui/<path:path>')
    async def send_js(path):
        resp = await send_from_directory('ui', path)
        # Cache bust XXL
        resp.cache_control.no_cache = True
        resp.cache_control.no_store = True
        resp.cache_control.max_age  = None
        resp.cache_control.must_revalidate = True
        return resp

    @app.route("/api/resolve/<name>")
    async def resolve(name):
        try:
            return jsonify(success=True, addrs=[
                info[4][0]
                for info in socket.getaddrinfo(name, 0, 0, socket.SOCK_STREAM)
            ])
        except socket.gaierror as err:
            return jsonify(success=False, error=str(err))

    @app.route("/api/targets", methods=["GET", "POST"])
    async def targets():
        if request.method == "GET":
            targets = []

            for _, name, addr in mp.iter_targets():
                targetinfo = mp.get_target_info(addr, name)
                loss = 0
                if targetinfo["sent"]:
                    loss = (targetinfo["sent"] - targetinfo["recv"]) / targetinfo["sent"] * 100
                targets.append(
                    dict(
                        targetinfo,
                        name=targetinfo["name"][:24],
                        succ=100 - loss,
                        loss=loss,
                        avg15m=targetinfo.get("avg15m", 0),
                        avg6h =targetinfo.get("avg6h",  0),
                        avg24h=targetinfo.get("avg24h", 0),
                    )
                )

            return jsonify(success=True, targets=targets)

        elif request.method == "POST":
            request_json = await request.get_json()
            if "target" not in request_json:
                return "missing target", 400

            target = request_json["target"]
            added = []

            if "@" not in target:
                for info in socket.getaddrinfo(target, 0, 0, socket.SOCK_STREAM):
                    target_with_addr = "%s@%s" % (target, info[4][0])
                    mp.add_target(target_with_addr)
                    added.append(target_with_addr)
            else:
                mp.add_target(target)
                added.append(target)

            return jsonify(success=True, targets=added)

    @app.route("/api/targets/<target>", methods=["PATCH", "PUT", "DELETE"])
    async def edit_target(target):
        if request.method == "DELETE":
            mp.remove_target(target)
            return jsonify(success=True)

        return jsonify(success=False)

    @app.route("/api/stats", methods=["DELETE"])
    async def clear_stats():
        mp.clear_stats()
        return jsonify(success=True)

    @app.route("/histogram/<node>/<target>.png")
    async def histogram(node, target):
        prom_url = os.environ.get("MESHPING_PROMETHEUS_URL")
        if prom_url is None:
            abort(503)

        prom_query = os.environ.get(
            "MESHPING_PROMETHEUS_QUERY",
            'increase(meshping_pings_bucket{instance="%(pingnode)s",name="%(name)s",target="%(addr)s"}[1h])'
        )

        if "@" in target:
            name, addr = target.split("@")
        else:
            for _, name, addr in mp.iter_targets():
                if name == target or addr == target:
                    break
            else:
                abort(400)

        async with httpx.AsyncClient() as client:
            response = (await client.get(prom_url + "/api/v1/query_range", timeout=2, params={
                "query": prom_query % dict(pingnode=node, name=name, addr=addr),
                "start": time() - 3 * 24 * 60 * 60,
                "end":   time(),
                "step":  3600,
            })).json()

        if response["status"] != "success":
            abort(500)
        if not response["data"]["result"]:
            abort(404)

        img = render(response)
        img_io = BytesIO()
        img.save(img_io, 'png')
        img_io.seek(0)
        return await send_file(img_io, mimetype='image/png')
