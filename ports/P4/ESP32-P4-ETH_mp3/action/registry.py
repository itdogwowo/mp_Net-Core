# /action/registry.py
# 統一註冊入口：把各 action 模組掛上去

from action import file_actions
# from action import fs_actions
from action import status_actions
from action import stream_actions
from action import sys_actions
from action import heartbeat_actions
from action import bench_actions
from action import now_actions
from action import hw_actions
from action import waiting_to_trash_actions
from action import net_actions
from action import pixel_actions
from action import audio_actions

def register_all(app):
    file_actions.register(app)
#     fs_actions.register(app)
    status_actions.register(app)
    stream_actions.register(app)
    sys_actions.register(app)
    heartbeat_actions.register(app)
    bench_actions.register(app)
    now_actions.register(app)
    hw_actions.register(app)
    waiting_to_trash_actions.register(app)
    net_actions.register(app)
    pixel_actions.register(app)
    audio_actions.register(app)
