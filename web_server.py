import os
import sys

from dotenv import load_dotenv
from openai import AsyncOpenAI

# --- AI & Notification Configuration ---
load_dotenv()


def _is_production_like() -> bool:
    env_name = (os.getenv("APP_ENV") or "").lower()
    if env_name in {"prod", "production"}:
        return True
    return os.getenv("RUNNING_IN_DOCKER", "false").lower() == "true"


def _require_env(name: str, *, allow_blank: bool = False) -> str:
    value = os.getenv(name)
    if value is None or (not allow_blank and not str(value).strip()):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


# --- File Paths & Directories ---
STATE_FILE = "xianyu_state.json"
IMAGE_SAVE_DIR = "images"
CONFIG_FILE = "config.json"
os.makedirs(IMAGE_SAVE_DIR, exist_ok=True)

# 任务隔离的临时图片目录前缀
TASK_IMAGE_DIR_PREFIX = "task_images_"

# --- API URL Patterns ---
API_URL_PATTERN = "h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search"
DETAIL_API_URL_PATTERN = "h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail"

# --- Environment Variables ---
API_KEY = os.getenv("OPENAI_API_KEY", "")
BASE_URL = os.getenv("OPENAI_BASE_URL", "")
MODEL_NAME = os.getenv("OPENAI_MODEL_NAME", "")
PROXY_URL = os.getenv("PROXY_URL", "")
NTFY_TOPIC_URL = os.getenv("NTFY_TOPIC_URL", "")
GOTIFY_URL = os.getenv("GOTIFY_URL", "")
GOTIFY_TOKEN = os.getenv("GOTIFY_TOKEN", "")
BARK_URL = os.getenv("BARK_URL", "")
WX_BOT_URL = os.getenv("WX_BOT_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_METHOD = os.getenv("WEBHOOK_METHOD", "POST").upper()
WEBHOOK_HEADERS = os.getenv("WEBHOOK_HEADERS")
WEBHOOK_CONTENT_TYPE = os.getenv("WEBHOOK_CONTENT_TYPE", "JSON").upper()
WEBHOOK_QUERY_PARAMETERS = os.getenv("WEBHOOK_QUERY_PARAMETERS")
WEBHOOK_BODY = os.getenv("WEBHOOK_BODY")
PCURL_TO_MOBILE = os.getenv("PCURL_TO_MOBILE", "false").lower() == "true"
RUN_HEADLESS = os.getenv("RUN_HEADLESS", "true").lower() != "false"
LOGIN_IS_EDGE = os.getenv("LOGIN_IS_EDGE", "false").lower() == "true"
RUNNING_IN_DOCKER = os.getenv("RUNNING_IN_DOCKER", "false").lower() == "true"
AI_DEBUG_MODE = os.getenv("AI_DEBUG_MODE", "false").lower() == "true"
SKIP_AI_ANALYSIS = os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true"
ENABLE_THINKING = os.getenv("ENABLE_THINKING", "false").lower() == "true"

if _is_production_like():
    try:
        _require_env("WEB_USERNAME")
        _require_env("WEB_PASSWORD")
        if os.getenv("WEB_USERNAME", "").strip() == "admin" and os.getenv("WEB_PASSWORD", "").strip() == "admin123":
            raise RuntimeError("Default credentials are forbidden in production environments.")
    except RuntimeError as exc:
        print(f"生产环境安全检查失败: {exc}")
        raise

# --- Headers ---
IMAGE_DOWNLOAD_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) Gecko/20100101 Firefox/139.0',
    'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}

# --- Client Initialization ---
# 检查配置是否齐全
if not all([BASE_URL, MODEL_NAME]):
    print("警告：未在 .env 文件中完整设置 OPENAI_BASE_URL 和 OPENAI_MODEL_NAME。AI相关功能可能无法使用。")
    client = None
else:
    try:
        if PROXY_URL:
            print(f"正在为AI请求使用HTTP/S代理: {PROXY_URL}")
            os.environ['HTTP_PROXY'] = PROXY_URL
            os.environ['HTTPS_PROXY'] = PROXY_URL

        client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)
    except Exception as e:
        print(f"初始化 OpenAI 客户端时出错: {e}")
        client = None

# 检查AI客户端是否成功初始化
if not client:
    pass

# 检查关键配置
if not all([BASE_URL, MODEL_NAME]) and 'prompt_generator.py' in sys.argv[0]:
    sys.exit("错误：请确保在 .env 文件中完整设置了 OPENAI_BASE_URL 和 OPENAI_MODEL_NAME。(OPENAI_API_KEY 对于某些服务是可选的)")


def get_ai_request_params(**kwargs):
    """
    构建AI请求参数，根据ENABLE_THINKING环境变量决定是否添加enable_thinking参数
    """
    if ENABLE_THINKING:
        kwargs["extra_body"] = {"enable_thinking": False}
    return kwargs
