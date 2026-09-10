#!/home/www/revfi/venv/bin/python3
"""Apache CGI front-end for RezFi (adapted from ~/freshfi/freshfi_proxy.cgi).

IONOS SSH cannot see or keep Apache-jail gunicorn. This CGI proxies the
vhost to 127.0.0.1:8099 and can start/stop only the [revfi] workers.
Never kill FreshFi (8090 / --name freshfi / /home/www/freshfi).
"""
import hmac
import json
import os
import sys
import traceback
from urllib.error import URLError
from urllib.parse import parse_qs
from urllib.request import HTTPErrorProcessor, Request, build_opener

APPDIR = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "revfi"
DEFAULT_BIND = "http://127.0.0.1:8099"
PUBLIC_HOST = "revfi.karyva.ai"


def _log(msg):
    path = os.path.join(APPDIR, "data", "cgi_hit.log")
    try:
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "a") as fh:
            fh.write(msg + "\n")
    except Exception:
        pass


def _bind():
    path = os.path.join(APPDIR, "tmp", "revfi.bind")
    bind = DEFAULT_BIND
    try:
        with open(path, "r") as fh:
            line = fh.read().strip()
        if line:
            if "://" not in line:
                line = "http://" + line
            bind = line
    except Exception:
        pass
    # FreshFi owns 8090. Never proxy RezFi there even if a leftover bind file says so.
    if bind.rstrip("/").endswith(":8090"):
        _log("ignoring leftover 8090 bind file; using %s" % DEFAULT_BIND)
        bind = DEFAULT_BIND
    return bind


def _bind_host_port(bind):
    host_port = bind.split("://", 1)[-1]
    host, port = "127.0.0.1", 8099
    if ":" in host_port:
        host, port_s = host_port.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            pass
    elif host_port:
        host = host_port
    if port == 8090:
        port = 8099
    return host, port


def _port_open(host, port):
    import socket

    sock = socket.socket()
    sock.settimeout(1)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _gunicorn_healthy(bind):
    """True if gunicorn answers /health without a inherited-SCRIPT_NAME error."""
    opener = build_opener(_PassThrough)
    try:
        resp = opener.open(bind.rstrip("/") + "/health", timeout=3)
        body = resp.read() or b""
        if b"SCRIPT_NAME" in body:
            return False
        return (resp.getcode() or 0) == 200
    except URLError:
        return False
    except Exception as err:
        _log("health probe failed: %s" % err)
        return False


def _is_revfi_cmd(cmd):
    """Only RezFi workers. Never FreshFi in the shared Apache jail."""
    if b"/home/www/freshfi" in cmd or b"--name freshfi" in cmd or b"[freshfi]" in cmd:
        return False
    return (
        b"--name revfi" in cmd
        or b"[revfi]" in cmd
        or b"/home/www/revfi" in cmd
        or (APPDIR.encode("utf-8") + b"/") in cmd
    )


def _kill_stale_gunicorn():
    """Kill RezFi gunicorn visible in this jail (Apache CGI can see workers SSH cannot)."""
    killed = 0
    proc = "/proc"
    try:
        pids = os.listdir(proc)
    except OSError:
        pids = []
    mypid = os.getpid()
    for name in pids:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == mypid:
            continue
        cmd_path = os.path.join(proc, name, "cmdline")
        try:
            with open(cmd_path, "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ")
        except OSError:
            continue
        if b"gunicorn" not in cmd and b"passenger_wsgi" not in cmd:
            continue
        if not _is_revfi_cmd(cmd):
            continue
        try:
            os.kill(pid, 15)
            killed += 1
        except OSError:
            pass
    if killed:
        import time
        time.sleep(0.4)
        for name in os.listdir(proc) if os.path.isdir(proc) else []:
            if not name.isdigit():
                continue
            try:
                with open(os.path.join(proc, name, "cmdline"), "rb") as fh:
                    cmd = fh.read().replace(b"\x00", b" ")
            except OSError:
                continue
            if b"gunicorn" not in cmd:
                continue
            if not _is_revfi_cmd(cmd):
                continue
            try:
                os.kill(int(name), 9)
            except OSError:
                pass
        _log("killed stale revfi gunicorn pids=%s" % killed)
    pid_file = os.path.join(APPDIR, "tmp", "revfi.pid")
    try:
        os.remove(pid_file)
    except OSError:
        pass
    return killed


def _stopped_path():
    return os.path.join(APPDIR, "tmp", "revfi.stopped")


def _ctl_token_path():
    return os.path.join(APPDIR, "data", ".ctl_token")


def _is_stopped():
    return os.path.isfile(_stopped_path())


def _set_stopped(flag):
    path = _stopped_path()
    if flag:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            pass
        try:
            with open(path, "w") as fh:
                fh.write("stopped\n")
        except OSError as err:
            _log("could not write stop hold: %s" % err)
            return False
        return True
    try:
        os.remove(path)
    except OSError:
        pass
    return True


def _ctl_token():
    try:
        with open(_ctl_token_path(), "r") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _query_token(uri):
    qs = os.environ.get("QUERY_STRING") or os.environ.get("REDIRECT_QUERY_STRING") or ""
    if uri and "?" in uri:
        qs = uri.split("?", 1)[1]
    params = parse_qs(qs, keep_blank_values=True)
    token = (params.get("token") or [""])[0]
    return token or (os.environ.get("HTTP_X_CTL_TOKEN") or "")


def _ctl_action(uri):
    path = (uri or "/").split("?", 1)[0].rstrip("/") or "/"
    marker = "/__ctl__/"
    idx = path.find(marker)
    if idx < 0:
        if path.endswith("/__ctl__") or path == "/__ctl__":
            return "status"
        return None
    action = path[idx + len(marker):].strip("/")
    if action in ("stop", "start", "status"):
        return action
    return None


def _ctl_authorized(provided):
    expected = _ctl_token()
    if not expected or not provided:
        return False
    try:
        return hmac.compare_digest(expected, provided)
    except (TypeError, ValueError):
        return False


def _json_response(status, obj):
    data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    out = _out()
    out.write(("Status: %s\r\n" % status).encode("ascii", "replace"))
    out.write(b"Content-Type: application/json\r\n")
    out.write(b"Cache-Control: no-store\r\n\r\n")
    out.write(data)


def _db_snapshot():
    import sqlite3

    path = os.path.join(APPDIR, "data", "revfi.db")
    if not os.path.isfile(path):
        path = os.path.join(APPDIR, "data", "aifinance.db")
    out = {"db": path, "exists": os.path.isfile(path)}
    if not out["exists"]:
        return out
    try:
        conn = sqlite3.connect(path)
        try:
            out["restaurants"] = [
                row[0] for row in conn.execute("SELECT id FROM restaurants")
            ]
            out["line_items"] = {
                (row[0] or ""): int(row[1] or 0)
                for row in conn.execute(
                    "SELECT restaurant_id, COUNT(*) FROM line_items GROUP BY restaurant_id"
                )
            }
            out["users"] = [
                {"username": row[0], "restaurant_id": row[1]}
                for row in conn.execute("SELECT username, restaurant_id FROM users")
            ]
        finally:
            conn.close()
    except Exception as err:
        out["error"] = str(err)
    return out


def _status_snapshot(bind=None):
    bind = bind or _bind()
    host, port = _bind_host_port(bind)
    pid = None
    pid_file = os.path.join(APPDIR, "tmp", "revfi.pid")
    try:
        with open(pid_file, "r") as fh:
            raw = fh.read().strip()
        pid = int(raw) if raw else None
    except (OSError, ValueError):
        pid = None
    port_open = _port_open(host, port)
    return {
        "app": APP_NAME,
        "bind": bind,
        "port_open": port_open,
        "healthy": _gunicorn_healthy(bind) if port_open else False,
        "stopped": _is_stopped(),
        "pidfile": pid,
        **_db_snapshot(),
    }


def _handle_ctl(uri):
    """Token-protected stop/start from SSH. Handle before auto-respawn."""
    action = _ctl_action(uri)
    if action is None:
        return False
    _log("ctl %s" % action)
    if not _ctl_token():
        _json_response("503 Service Unavailable", {
            "ok": False,
            "error": "ctl token not configured",
        })
        return True
    if not _ctl_authorized(_query_token(uri)):
        _json_response("403 Forbidden", {
            "ok": False,
            "error": "forbidden",
            "hint": "Do not open this URL in a browser. From SSH: cd /home/www/revfi && ./start.sh stop",
        })
        return True
    bind = _bind()
    if action == "stop":
        _set_stopped(True)
        killed = _kill_stale_gunicorn()
        snap = _status_snapshot(bind)
        _json_response("200 OK", {
            "ok": True,
            "action": "stop",
            "killed": killed,
            **snap,
        })
        return True
    if action == "start":
        _set_stopped(False)
        started = _ensure_gunicorn(bind)
        snap = _status_snapshot(bind)
        _json_response(
            "200 OK" if started else "502 Bad Gateway",
            {"ok": bool(started), "action": "start", **snap},
        )
        return True
    _json_response("200 OK", {"ok": True, "action": "status", **_status_snapshot(bind)})
    return True


def _stopped_response():
    _out().write(
        b"Status: 503 Service Unavailable\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Cache-Control: no-store\r\n\r\n"
        b"revfi is stopped; start with: ./start.sh start\n"
    )


def _ensure_gunicorn(bind):
    """Start daemon gunicorn if Apache cannot reach it.

    IONOS webspace kills processes started from an SSH login when that
    session ends. Processes spawned by this CGI belong to Apache and stay up.
    """
    import fcntl
    import subprocess
    import time

    if _is_stopped():
        _log("hold file present; not starting gunicorn")
        return False

    host, port = _bind_host_port(bind)
    if port == 8090:
        _log("refusing to bind RezFi on FreshFi port 8090")
        host, port = "127.0.0.1", 8099
        bind = "http://127.0.0.1:8099"
    if _port_open(host, port):
        if _gunicorn_healthy(bind):
            return True
        _log("gunicorn on %s:%s is unhealthy; restarting" % (host, port))
        _kill_stale_gunicorn()

    tmp = os.path.join(APPDIR, "tmp")
    logs = os.path.join(APPDIR, "logs")
    try:
        os.makedirs(tmp, exist_ok=True)
        os.makedirs(logs, exist_ok=True)
    except OSError as err:
        _log("ensure mkdir failed: %s" % err)
        return False

    bind_path = os.path.join(tmp, "revfi.bind")
    try:
        with open(bind_path, "w") as fh:
            fh.write("%s:%s\n" % (host, port))
    except OSError:
        pass

    python = os.path.join(APPDIR, "venv", "bin", "python3")
    if not os.access(python, os.X_OK):
        python = sys.executable
    lock_path = os.path.join(tmp, "revfi.start.lock")
    pid_file = os.path.join(tmp, "revfi.pid")
    cmd = [
        python, "-m", "gunicorn", "passenger_wsgi:application",
        "--chdir", APPDIR,
        "--bind", "%s:%s" % (host, port),
        "--workers", "2",
        "--timeout", "120",
        "--name", APP_NAME,
        "--daemon",
        "--pid", pid_file,
        "--access-logfile", os.path.join(logs, "revfi.log"),
        "--error-logfile", os.path.join(logs, "revfi-error.log"),
    ]
    try:
        lock = open(lock_path, "w")
    except OSError as err:
        _log("ensure lock failed: %s" % err)
        return False
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if _port_open(host, port):
            return True
        _log("starting gunicorn: %s" % " ".join(cmd))
        # CGI exports SCRIPT_NAME=/revfi_proxy.cgi; gunicorn would apply that
        # to every request as "path does not start with SCRIPT_NAME".
        keep = (
            "PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE",
            "VIRTUAL_ENV", "TMPDIR",
        )
        clean_env = {key: os.environ[key] for key in keep if key in os.environ}
        clean_env["HOME"] = os.environ.get("HOME") or "/home/www"
        clean_env["PATH"] = os.environ.get("PATH") or "/usr/bin:/bin"
        db_path = os.path.join(APPDIR, "data", "revfi.db")
        if not os.path.isfile(db_path):
            db_path = os.path.join(APPDIR, "data", "aifinance.db")
        clean_env["REZFI_DB_PATH"] = db_path
        if "freshfi" in clean_env["REZFI_DB_PATH"].replace("\\", "/").lower():
            _log("refusing FreshFi database path")
            return False
        subprocess.Popen(
            cmd,
            cwd=APPDIR,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=clean_env,
        )
        for _ in range(50):
            if _port_open(host, port):
                return True
            time.sleep(0.2)
        ok = _port_open(host, port)
        _log("gunicorn listening=%s" % ok)
        return ok
    except Exception as err:
        _log("gunicorn spawn failed: %s" % err)
        return False
    finally:
        try:
            lock.close()
        except Exception:
            pass


def _target_uri():
    for key in ("REDIRECT_REQUEST_URI", "REDIRECT_URL", "REQUEST_URI"):
        uri = os.environ.get(key) or ""
        if not uri:
            continue
        if key == "REDIRECT_URL":
            qs = os.environ.get("REDIRECT_QUERY_STRING") or ""
            if qs and "?" not in uri:
                uri = uri + "?" + qs
        path_only = uri.split("?", 1)[0]
        base = path_only.rstrip("/").split("/")[-1]
        if base in ("revfi_proxy.cgi", "revfi_proxy.php", "freshfi_proxy.cgi", "freshfi_proxy.php"):
            continue
        return uri
    return "/"


def _method():
    return (
        os.environ.get("REDIRECT_REQUEST_METHOD")
        or os.environ.get("REQUEST_METHOD")
        or "GET"
    )


class _PassThrough(HTTPErrorProcessor):
    def http_response(self, request, response):
        return response

    https_response = http_response


def _forward_headers():
    skip = {
        "HTTP_HOST", "HTTP_CONNECTION", "HTTP_CONTENT_LENGTH",
        "HTTP_ACCEPT_ENCODING",
    }
    headers = {
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": os.environ.get("REMOTE_ADDR") or "",
        "X-Forwarded-Host": os.environ.get("HTTP_HOST") or PUBLIC_HOST,
        "X-Forwarded-Port": "443",
    }
    for key, val in os.environ.items():
        if key.startswith("HTTP_") and key not in skip:
            name = key[5:].replace("_", "-").title()
            headers[name] = val
    ctype = os.environ.get("CONTENT_TYPE")
    if ctype:
        headers["Content-Type"] = ctype
    return headers


def _out():
    return sys.stdout.buffer


def _write_response(status, hdrs, data):
    out = _out()
    out.write(("Status: %s\r\n" % status).encode("ascii", "replace"))
    skip_h = {"transfer-encoding", "connection", "content-encoding", "status"}
    cookies = []
    if hdrs is not None and hasattr(hdrs, "get_all"):
        try:
            cookies = hdrs.get_all("Set-Cookie") or []
        except Exception:
            cookies = []
    items = []
    try:
        items = list(hdrs.items()) if hdrs is not None else []
    except Exception:
        items = []
    for key, val in items:
        lk = str(key).lower()
        if lk in skip_h or lk == "set-cookie":
            continue
        out.write(("%s: %s\r\n" % (key, val)).encode("utf-8", "replace"))
    for cookie in cookies:
        out.write(("Set-Cookie: %s\r\n" % cookie).encode("utf-8", "replace"))
    out.write(b"\r\n")
    out.write(data or b"")


def main():
    uri = _target_uri()
    method = _method()
    bind = _bind()
    if _handle_ctl(uri):
        return
    if _is_stopped():
        _stopped_response()
        return
    try:
        length = int(os.environ.get("CONTENT_LENGTH") or os.environ.get("REDIRECT_CONTENT_LENGTH") or "0")
    except ValueError:
        length = 0
    body = None
    if method not in ("GET", "HEAD") and length > 0:
        body = sys.stdin.buffer.read(length)
    _log("%s %s len=%s -> %s%s" % (method, uri, length, bind, uri))
    if not _ensure_gunicorn(bind):
        msg = "revfi cgi: gunicorn is not listening at %s\n" % bind
        _log(msg.strip())
        _out().write(
            b"Status: 502 Bad Gateway\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        )
        _out().write(msg.encode("utf-8"))
        return
    req = Request(bind + uri, data=body, headers=_forward_headers(), method=method)
    opener = build_opener(_PassThrough)
    try:
        resp = opener.open(req, timeout=120)
        status = resp.getcode() or 200
        hdrs = resp.info()
        data = resp.read()
    except URLError as err:
        _log("urlerror %s; retrying after ensure" % err)
        _ensure_gunicorn(bind)
        try:
            resp = opener.open(req, timeout=120)
            status = resp.getcode() or 200
            hdrs = resp.info()
            data = resp.read()
        except URLError as err2:
            msg = "revfi cgi: cannot reach gunicorn at %s (%s)\n" % (bind, err2)
            _log("urlerror %s" % err2)
            _out().write(
                b"Status: 502 Bad Gateway\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            )
            _out().write(msg.encode("utf-8"))
            return
    _write_response(status, hdrs, data)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _log(traceback.format_exc())
        _out().write(
            b"Status: 500 Internal Server Error\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"revfi cgi crashed; see data/cgi_hit.log\n"
        )
