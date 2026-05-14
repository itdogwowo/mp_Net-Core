# /action/registry.py
# 統一註冊入口：把各 action 模組掛上去

from action import file_actions
# from action import fs_actions
from action import status_actions
from action import stream_actions
from action import sys_actions 
from action import heartbeat_actions
from action import ram_bench_actions
from action import jpeg_actions
from action import mp4_actions

def register_all(app):
    file_actions.register(app)
#     fs_actions.register(app)
    status_actions.register(app)
    stream_actions.register(app)
    sys_actions.register(app)
    heartbeat_actions.register(app)
    ram_bench_actions.register(app)
    jpeg_actions.register(app)
    mp4_actions.register(app)
