"""A tiny pretend network, to try the agent without a real system:

    CANAH_PROVIDERS=examples.demo_network:provider python -m canah chat
"""
import json

from canah import FunctionProvider

DEVICES = {"r1": {"status": "up", "cpu": 12}, "r2": {"status": "down", "cpu": None}, "sw1": {"status": "up", "cpu": 40}}


def list_devices() -> str:
    """List every device with its status (up/down)."""
    return json.dumps({n: d["status"] for n, d in DEVICES.items()})


def get_device(name: str) -> str:
    """Read one device's full state: status and CPU load in percent."""
    if name not in DEVICES:
        return f"no device called {name}; known: {', '.join(DEVICES)}"
    return json.dumps({name: DEVICES[name]})


def restart_device(name: str) -> str:
    """Restart a device. It comes back up."""
    if name not in DEVICES:
        return f"no device called {name}"
    DEVICES[name] = {"status": "up", "cpu": 5}
    return f"{name} restarted; it is up"


def provider() -> FunctionProvider:
    p = FunctionProvider()
    p.add(list_devices)
    p.add(get_device)
    p.add(restart_device, write=True)
    return p
