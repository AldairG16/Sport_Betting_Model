"""
Supervisor del dashboard (dashboard/supervisor.py, v1.6.0): con procesos,
puerto y reloj falsos. El 24-sep-26 el servidor murió y la página mostró
"HTTP 0" hasta que alguien lo abrió a mano; ahora vuelve solo.
"""

import dashboard.supervisor as sv


class FakeChild:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


class World:
    """Estado simulado: el puerto, la salud del servidor, el reloj."""

    def __init__(self, healthy=True, busy=False, foreign_image="BettingDashboard.exe"):
        self.t = 0.0
        self.healthy = healthy
        self.busy = busy
        self.spawned, self.killed, self.log, self.browser = [], [], [], 0
        self.foreign_image = foreign_image
        self.sup = sv.Supervisor(
            spawn=self.spawn, is_healthy=lambda: self.healthy, is_port_busy=lambda: self.busy,
            kill=self.kill, find_listener=lambda: 999, image_of=lambda pid: self.foreign_image,
            browser=self.open_browser, log=self.log.append, sleep=self.sleep, clock=lambda: self.t)

    def spawn(self):
        child = FakeChild(100 + len(self.spawned))
        self.spawned.append(child)
        self.busy = True
        return child

    def kill(self, pid):
        self.killed.append(pid)
        self.busy = False

    def open_browser(self):
        self.browser += 1

    def sleep(self, s):
        self.t += s

    def tick(self, n=1):
        for _ in range(n):
            self.sup.step()
            self.sleep(sv.CHECK_EVERY_S)


def test_starts_the_server_and_opens_the_browser_once():
    w = World()
    w.tick(4)
    assert len(w.spawned) == 1 and w.browser == 1 and w.killed == []


def test_a_crashed_server_comes_back_with_backoff():
    w = World()
    w.tick(2)
    w.spawned[0].returncode = 1          # se cae
    w.busy = False
    t0 = w.t
    w.tick()
    assert len(w.spawned) == 2
    assert w.t - t0 >= sv.BACKOFF_S[0]
    assert any("se cerró (código 1)" in m for m in w.log)
    assert w.browser == 1                # no vuelve a abrir el navegador


def test_repeated_crashes_wait_longer_each_time():
    w = World(healthy=False)
    waits = []
    for i in range(3):
        w.tick()
        w.spawned[-1].returncode = 1
        w.busy = False
        before = w.t
        w.sup.step()
        waits.append(w.t - before)
    assert waits == list(sv.BACKOFF_S[:3])


def test_a_hung_server_is_killed_with_its_tree_and_restarted():
    w = World()
    w.tick(2)
    w.healthy = False                    # deja de responder
    w.t += sv.STARTUP_GRACE_S
    w.tick(sv.MAX_FAILS)
    assert w.killed == [w.spawned[0].pid]
    w.healthy = True
    w.tick()
    assert len(w.spawned) == 2


def test_a_slow_start_is_not_a_hang():
    w = World(healthy=False)
    w.tick(int(sv.STARTUP_GRACE_S // sv.CHECK_EVERY_S) - 1)    # todavía arrancando
    assert w.killed == [] and len(w.spawned) == 1


def test_adopts_a_healthy_server_already_running():
    w = World(busy=True)                 # p. ej. el .exe anterior
    w.tick(3)
    assert w.spawned == [] and w.browser == 1


def test_a_hung_old_dashboard_on_the_port_is_replaced():
    w = World(busy=True, healthy=False)
    w.tick(sv.MAX_FAILS)
    assert w.killed == [999]
    w.healthy = True
    w.tick()
    assert len(w.spawned) == 1


def test_never_kills_another_program_on_the_port():
    w = World(busy=True, healthy=False, foreign_image="nginx.exe")
    w.tick(sv.MAX_FAILS * 2)
    assert w.killed == [] and w.spawned == []
    assert any("otro programa" in m for m in w.log)


def test_shutdown_stops_the_server():
    w = World()
    w.tick()
    w.sup.shutdown()
    assert w.killed == [w.spawned[0].pid]


def test_listening_pid_ignores_the_language_of_windows():
    en = ("  TCP    127.0.0.1:5050         0.0.0.0:0              LISTENING       37176\n"
          "  TCP    127.0.0.1:5050         127.0.0.1:61234        ESTABLISHED     37176\n")
    es = "  TCP    127.0.0.1:5050         0.0.0.0:0              ESCUCHANDO      4242\n"
    other = "  TCP    127.0.0.1:15050        0.0.0.0:0              LISTENING       7\n"
    assert sv.parse_listening_pid(en) == 37176
    assert sv.parse_listening_pid(es) == 4242
    assert sv.parse_listening_pid(other) is None


def test_health_endpoint_needs_no_database():
    import dashboard.app as dash
    r = dash.app.test_client().get("/api/health")
    assert r.status_code == 200 and r.get_json()["ok"] is True
