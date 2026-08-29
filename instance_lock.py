"""กันบอทรันซ้อน: จอง TCP port บน localhost เป็น lock ระดับเครื่อง (port จากบอท US bot/instance_lock.py)

บอทไทยไม่เคยมี lock — รัน "เริ่ม Bot.bat" ซ้อน หรือรันสำเนา 2 โฟลเดอร์พร้อมกัน = สอง instance
แย่ง getUpdates → telegram.error.Conflict (US เจอจริง 65 ครั้ง 2026-08-20) ใช้ socket แทน lockfile
เพราะ process ตาย (crash/kill) แล้ว OS คืน port ให้เอง ไม่มี stale lock ค้าง
"""
import socket

LOCK_PORT = 48952               # port ประจำบอทไทย (US 48962 · HK 48972 — รันคู่กันบนเครื่องเดียวได้)


def acquire(port=LOCK_PORT):
    """จอง lock — คืน socket (ผู้เรียกถือ reference ไว้ตลอดอายุ process) · None = มีตัวอื่นถืออยู่"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        return None
    sock.listen(1)
    return sock
