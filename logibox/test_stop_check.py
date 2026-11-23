"""Local port ownership checks, including Windows exclusivity and failures."""
import errno
import os
import socket
import unittest
from unittest.mock import patch

import password_recovery as recovery


class StopCheckTests(unittest.TestCase):
    def test_free_port_needs_no_connection_or_timeout(self):
        with patch.object(recovery.socket, 'socket') as factory:
            probe = factory.return_value.__enter__.return_value
            recovery.ensure_stopped()
            probe.bind.assert_called_once_with(('127.0.0.1', 8766))
            probe.connect_ex.assert_not_called()
            probe.settimeout.assert_not_called()
            factory.return_value.__exit__.assert_called_once()

    def test_windows_uses_exclusive_bind(self):
        with patch.object(recovery.os, 'name', 'nt'), patch.object(recovery.socket, 'SO_EXCLUSIVEADDRUSE', -5, create=True), patch.object(recovery.socket, 'socket') as factory:
            probe = factory.return_value.__enter__.return_value
            recovery.ensure_stopped()
            probe.setsockopt.assert_called_once_with(socket.SOL_SOCKET, -5, 1)
            self.assertEqual([call[0] for call in probe.method_calls], ['setsockopt', 'bind'])

    def test_busy_port_is_rejected(self):
        for code in (errno.EADDRINUSE, 10048):
            with self.subTest(code=code), patch.object(recovery.socket, 'socket') as factory:
                factory.return_value.__enter__.return_value.bind.side_effect = OSError(code, 'busy')
                with self.assertRaisesRegex(RuntimeError, '使用中'):
                    recovery.ensure_stopped()

    def test_unexpected_failure_is_not_treated_as_stopped(self):
        for code in (errno.EACCES, 10013, errno.EIO):
            with self.subTest(code=code), patch.object(recovery.socket, 'socket') as factory:
                factory.return_value.__enter__.return_value.bind.side_effect = OSError(code, 'denied')
                with self.assertRaisesRegex(RuntimeError, 'denied'):
                    recovery.ensure_stopped()

    def test_socket_creation_failure_is_reported(self):
        with patch.object(recovery.socket, 'socket', side_effect=OSError(errno.EACCES, 'denied')):
            with self.assertRaisesRegex(RuntimeError, 'denied'):
                recovery.ensure_stopped()

    def test_real_legacy_listener_is_rejected(self):
        for address in ('127.0.0.1', '0.0.0.0'):
            with self.subTest(address=address), socket.socket() as listener:
                listener.bind((address, 8766))
                listener.listen()
                with self.assertRaisesRegex(RuntimeError, '使用中'):
                    recovery.ensure_stopped()

    def test_real_free_port_is_released_after_check(self):
        recovery.ensure_stopped()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 8766))


if __name__ == '__main__':
    unittest.main()
