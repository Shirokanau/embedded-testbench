"""传输层：真实串口与虚拟回环串口。"""
from .loopback import FaultConfig, LoopbackTransport, Transport
from .serial_port import DBUS_SERIAL_KWARGS, SerialTransport, SerialUnavailable

__all__ = [
    "Transport", "LoopbackTransport", "FaultConfig",
    "SerialTransport", "SerialUnavailable", "DBUS_SERIAL_KWARGS",
]
