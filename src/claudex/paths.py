"""claudex 读写的全部位置：配置根、运行态根与由它们派生的文件路径。

两个根目录可由 `CLAUDEX_CONFIG_DIR`、`CLAUDEX_STATE_DIR` 覆盖；所有路径都在调用时
求值，环境变量或 HOME 变化后立即生效。
"""

import os
from pathlib import Path

CONFIG_DIR_ENV = "CLAUDEX_CONFIG_DIR"
STATE_DIR_ENV = "CLAUDEX_STATE_DIR"


def config_dir() -> Path:
    """配置根目录：`CLAUDEX_CONFIG_DIR` 非空时取它，否则 `~/.config/claudex`。"""
    configured = os.environ.get(CONFIG_DIR_ENV)
    if configured:
        return Path(configured)
    return Path.home() / ".config" / "claudex"


def state_dir() -> Path:
    """运行态根目录：`CLAUDEX_STATE_DIR` 非空时取它，否则 `~/.local/state/claudex`。"""
    configured = os.environ.get(STATE_DIR_ENV)
    if configured:
        return Path(configured)
    return Path.home() / ".local" / "state" / "claudex"


def config_file() -> Path:
    """用户配置 `claudex.toml`。"""
    return config_dir() / "claudex.toml"


def gateway_base_file() -> Path:
    """网关非来源配置的底稿 `gateway.base.yaml`。"""
    return config_dir() / "gateway.base.yaml"


def settings_base_file() -> Path:
    """派生 Claude Code settings 的基底 `settings.base.json`。"""
    return config_dir() / "settings.base.json"


def client_key_file() -> Path:
    """网关下游 key。"""
    return config_dir() / "client.key"


def management_key_file() -> Path:
    """网关管理接口密码。"""
    return config_dir() / "management.key"


def keys_dir() -> Path:
    """通用来源上游 API key 所在目录。"""
    return config_dir() / "keys"


def source_key_file(source: str) -> Path:
    """来源 `source` 的上游 API key：`keys/<source>.key`。"""
    return keys_dir() / f"{source}.key"


def gateway_config_file() -> Path:
    """生成的网关配置 `gateway.yaml`（运行态根下）。"""
    return state_dir() / "gateway.yaml"


def gateway_pid_file() -> Path:
    """启动器记录的网关进程 `gateway.pid`（运行态根下）。"""
    return state_dir() / "gateway.pid"


def gateway_start_lock_file() -> Path:
    """启动器拉起网关时持有的锁 `gateway-start.lock`（运行态根下）。"""
    return state_dir() / "gateway-start.lock"


def gateway_bootstrap_log_file() -> Path:
    """启动器拉起网关时网关进程的输出 `gateway-bootstrap.log`（运行态根下）。"""
    return state_dir() / "gateway-bootstrap.log"


def gateway_logs_dir() -> Path:
    """网关日志与失败快照目录 `logs/`（运行态根下）。"""
    return state_dir() / "logs"


def quota_file() -> Path:
    """额度与余额的 last-good 缓存 `quota.json`（运行态根下）。"""
    return state_dir() / "quota.json"


def refresh_file() -> Path:
    """后台刷新的节流与尝试记录 `refresh.json`（运行态根下）。"""
    return state_dir() / "refresh.json"


def refresh_lock_file() -> Path:
    """后台刷新程序持有的锁 `refresh.lock`（运行态根下）。"""
    return state_dir() / "refresh.lock"


def gateway_release_file() -> Path:
    """网关最新发布版本与查询时刻 `gateway-release.json`（运行态根下）。"""
    return state_dir() / "gateway-release.json"


def rollouts_dir() -> Path:
    """受管升级与重启的状态记录目录 `rollouts/`（运行态根下）。"""
    return state_dir() / "rollouts"


def upgrade_lock_file() -> Path:
    """受管升级与重启的并发锁 `upgrade.lock`（运行态根下）。"""
    return state_dir() / "upgrade.lock"


def sessions_dir() -> Path:
    """会话快照目录（运行态根下）。"""
    return state_dir() / "sessions"


def catalog_file() -> Path:
    """OpenRouter 目录缓存（运行态根下）。"""
    return state_dir() / "catalog.json"


def auth_dir() -> Path:
    """网关 OAuth 凭据目录：`~/.local/share/claudex/auth`。"""
    return Path.home() / ".local" / "share" / "claudex" / "auth"
