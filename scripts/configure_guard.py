"""Configure monitoring while the widget is stopped; the panel is preferred."""
import argparse
import fcntl
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'backend'))
from service import Store

parser = argparse.ArgumentParser()
parser.add_argument('--mode', choices=['observe', 'auto', 'paused'], required=True)
parser.add_argument('--allow-reset', action='store_true', help='允许自动调度使用已有重置卡')
parser.add_argument('--wait', type=int, default=0, help='等待已退出小组件的监控进程释放锁，最多 45 秒')
args = parser.parse_args()
root = pathlib.Path.home()/'Library/Application Support/CodexQuota'
store = Store(root)
lease = (root/'controller.lock').open('a')
deadline = time.monotonic()+min(45, max(0, args.wait))
while True:
    try:
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        if time.monotonic() >= deadline:
            raise SystemExit('请先退出小组件，再应用启动设置。')
        time.sleep(.25)
store.put('auto_reset', args.allow_reset)
store.put('desired_mode', args.mode)
store.event('已更新监控模式与重置卡设置')
store.db.close()
print('监控设置已保存；打开小组件后生效。')
