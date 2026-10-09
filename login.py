import asyncio
import base64
import json
import os
import signal
import sys
from contextlib import asynccontextmanager
from typing import Optional

import aiofiles
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import dotenv_values
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from src.prompt_utils import generate_criteria, update_config_with_new_task


class Task(BaseModel):
    task_name: str
    enabled: bool
    keyword: str
    description: str
    max_pages: int
    personal_only: bool
    min_price: Optional[str] = None
    max_price: Optional[str] = None
    cron: Optional[str] = None
    ai_prompt_base_file: str
    ai_prompt_criteria_file: str
    is_running: Optional[bool] = False


class TaskUpdate(BaseModel):
    task_name: Optional[str] = None
    enabled: Optional[bool] = None
    keyword: Optional[str] = None
    description: Optional[str] = None
    max_pages: Optional[int] = None
    personal_only: Optional[bool] = None
    min_price: Optional[str] = None
    max_price: Optional[str] = None
    cron: Optional[str] = None
    ai_prompt_base_file: Optional[str] = None
    ai_prompt_criteria_file: Optional[str] = None
    is_running: Optional[bool] = None


class TaskGenerateRequest(BaseModel):
    task_name: str
    keyword: str
    description: str
    personal_only: bool = True
    min_price: Optional[str] = None
    max_price: Optional[str] = None
    max_pages: int = 3
    cron: Optional[str] = None

    @classmethod
    def validate_fields(cls, value):
        if len(value.task_name.strip()) == 0:
            raise ValueError("task_name cannot be empty")
        if len(value.keyword.strip()) == 0:
            raise ValueError("keyword cannot be empty")
        if len(value.description.strip()) == 0:
            raise ValueError("description cannot be empty")
        if len(value.task_name) > 200:
            raise ValueError("task_name is too long")
        if len(value.keyword) > 200:
            raise ValueError("keyword is too long")
        if len(value.description) > 4000:
            raise ValueError("description is too long")
        return value


class PromptUpdate(BaseModel):
    content: str


class LoginStateUpdate(BaseModel):
    content: str

    @classmethod
    def validate_fields(cls, value):
        if not value.content or len(value.content.strip()) == 0:
            raise ValueError("login state cannot be empty")
        if len(value.content) > 1_000_000:
            raise ValueError("login state is too large")
        try:
            json.loads(value.content)
        except json.JSONDecodeError as exc:
            raise ValueError("login state must be valid JSON") from exc
        return value


class NotificationSettings(BaseModel):
    NTFY_TOPIC_URL: Optional[str] = None
    GOTIFY_URL: Optional[str] = None
    GOTIFY_TOKEN: Optional[str] = None
    BARK_URL: Optional[str] = None
    WX_BOT_URL: Optional[str] = None
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None
    WEBHOOK_URL: Optional[str] = None
    WEBHOOK_METHOD: Optional[str] = "POST"
    WEBHOOK_HEADERS: Optional[str] = None
    WEBHOOK_CONTENT_TYPE: Optional[str] = "JSON"
    WEBHOOK_QUERY_PARAMETERS: Optional[str] = None
    WEBHOOK_BODY: Optional[str] = None
    PCURL_TO_MOBILE: Optional[bool] = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理应用的生命周期事件。"""
    await _set_all_tasks_stopped_in_config()
    await reload_scheduler_jobs()
    if not scheduler.running:
        scheduler.start()

    yield

    if scheduler.running:
        print("正在关闭调度器...")
        scheduler.shutdown()

    global scraper_processes
    if scraper_processes:
        print("Web服务器正在关闭，正在终止所有爬虫进程...")
        stop_tasks = [stop_task_process(task_id) for task_id in list(scraper_processes.keys())]
        await asyncio.gather(*stop_tasks)
        print("所有爬虫进程已终止。")

    await _set_all_tasks_stopped_in_config()


def load_notification_settings():
    """Load notification settings from .env file"""
    config = dotenv_values(".env")

    return {
        "NTFY_TOPIC_URL": config.get("NTFY_TOPIC_URL", ""),
        "GOTIFY_URL": config.get("GOTIFY_URL", ""),
        "GOTIFY_TOKEN": config.get("GOTIFY_TOKEN", ""),
        "BARK_URL": config.get("BARK_URL", ""),
        "WX_BOT_URL": config.get("WX_BOT_URL", ""),
        "TELEGRAM_BOT_TOKEN": config.get("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID": config.get("TELEGRAM_CHAT_ID", ""),
        "WEBHOOK_URL": config.get("WEBHOOK_URL", ""),
        "WEBHOOK_METHOD": config.get("WEBHOOK_METHOD", "POST"),
        "WEBHOOK_HEADERS": config.get("WEBHOOK_HEADERS", ""),
        "WEBHOOK_CONTENT_TYPE": config.get("WEBHOOK_CONTENT_TYPE", "JSON"),
        "WEBHOOK_QUERY_PARAMETERS": config.get("WEBHOOK_QUERY_PARAMETERS", ""),
        "WEBHOOK_BODY": config.get("WEBHOOK_BODY", ""),
        "PCURL_TO_MOBILE": config.get("PCURL_TO_MOBILE", "true").lower() == "true"
    }


def save_notification_settings(settings: dict):
    """Save notification settings to .env file"""
    env_file = ".env"
    env_lines = []

    if os.path.exists(env_file):
        with open(env_file, 'r', encoding='utf-8') as f:
            env_lines = f.readlines()

    setting_keys = [
        "NTFY_TOPIC_URL", "GOTIFY_URL", "GOTIFY_TOKEN", "BARK_URL",
        "WX_BOT_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WEBHOOK_URL",
        "WEBHOOK_METHOD", "WEBHOOK_HEADERS", "WEBHOOK_CONTENT_TYPE", "WEBHOOK_QUERY_PARAMETERS",
        "WEBHOOK_BODY", "PCURL_TO_MOBILE"
    ]

    existing_settings = {}
    for line in env_lines:
        if '=' in line and not line.strip().startswith('#'):
            key, value = line.split('=', 1)
            existing_settings[key.strip()] = value.strip()

    existing_settings.update(settings)

    with open(env_file, 'w', encoding='utf-8') as f:
        for key in setting_keys:
            value = existing_settings.get(key, "")
            if key == "PCURL_TO_MOBILE":
                f.write(f"{key}={str(value).lower()}\n")
            else:
                f.write(f"{key}={value}\n")

        for key, value in existing_settings.items():
            if key not in setting_keys:
                f.write(f"{key}={value}\n")


def load_ai_settings():
    """Load AI model settings from .env file"""
    config = dotenv_values(".env")

    return {
        "OPENAI_API_KEY": config.get("OPENAI_API_KEY", ""),
        "OPENAI_BASE_URL": config.get("OPENAI_BASE_URL", ""),
        "OPENAI_MODEL_NAME": config.get("OPENAI_MODEL_NAME", ""),
        "PROXY_URL": config.get("PROXY_URL", "")
    }


def save_ai_settings(settings: dict):
    """Save AI model settings to .env file"""
    env_file = ".env"
    env_lines = []

    if os.path.exists(env_file):
        with open(env_file, 'r', encoding='utf-8') as f:
            env_lines = f.readlines()

    setting_keys = [
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL_NAME", "PROXY_URL"
    ]

    existing_settings = {}
    for line in env_lines:
        if '=' in line and not line.strip().startswith('#'):
            key, value = line.split('=', 1)
            existing_settings[key.strip()] = value.strip()

    existing_settings.update(settings)

    with open(env_file, 'w', encoding='utf-8') as f:
        for key in setting_keys:
            value = existing_settings.get(key, "")
            f.write(f"{key}={value}\n")

        for key, value in existing_settings.items():
            if key not in setting_keys:
                f.write(f"{key}={value}\n")


app = FastAPI(title="闲鱼智能监控机器人", lifespan=lifespan)
security = HTTPBasic()


def get_auth_credentials():
    """从环境变量获取认证凭据，并拒绝默认弱凭据。"""
    username = os.getenv("WEB_USERNAME")
    password = os.getenv("WEB_PASSWORD")

    if not username or not password:
        raise RuntimeError("WEB_USERNAME and WEB_PASSWORD must be set.")
    if username == "admin" and password == "admin123":
        raise RuntimeError("Default credentials are forbidden.")
    return username, password


def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    """验证Basic认证凭据"""
    try:
        username, password = get_auth_credentials()
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is not configured properly",
            headers={"WWW-Authenticate": "Basic"},
        )

    if credentials.username == username and credentials.password == password:
        return credentials.username
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="认证失败",
        headers={"WWW-Authenticate": "Basic"},
    )


scraper_processes = {}
scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


class AuthenticatedStaticFiles(StaticFiles):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"").decode()

        if not authorization.startswith("Basic "):
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", b"Basic realm=Authorization Required"),
                    (b"content-type", b"text/plain"),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b"Authentication required",
            })
            return

        try:
            credentials = base64.b64decode(authorization[6:]).decode()
            username, password = credentials.split(":", 1)
            expected_username, expected_password = get_auth_credentials()
            if username != expected_username or password != expected_password:
                raise ValueError("Invalid credentials")
        except Exception:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", b"Basic realm=Authorization Required"),
                    (b"content-type", b"text/plain"),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b"Authentication failed",
            })
            return

        await super().__call__(scope, receive, send)


app.mount("/static", AuthenticatedStaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


async def run_single_task(task_id: int, task_name: str):
    print(f"定时任务触发: 正在为任务 '{task_name}' 启动爬虫...")
    log_file_handle = None
    try:
        await update_task_running_status(task_id, True)

        os.makedirs("logs", exist_ok=True)
        log_file_path = os.path.join("logs", "scraper.log")
        log_file_handle = open(log_file_path, 'a', encoding='utf-8')

        preexec_fn = os.setsid if sys.platform != "win32" else None
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"

        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "spider_v2.py", "--task-name", task_name,
            stdout=log_file_handle,
            stderr=log_file_handle,
            preexec_fn=preexec_fn,
            env=child_env
        )

        await process.wait()
        if process.returncode == 0:
            print(f"定时任务 '{task_name}' 执行成功。日志已写入 {log_file_path}")
        else:
            print(f"定时任务 '{task_name}' 执行失败。返回码: {process.returncode}。详情请查看 {log_file_path}")

    except Exception as e:
        print(f"启动定时任务 '{task_name}' 时发生错误: {e}")
    finally:
        if log_file_handle:
            log_file_handle.close()
        await update_task_running_status(task_id, False)


async def _set_all_tasks_stopped_in_config():
    try:
        async with aiofiles.open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            content = await f.read()
            if not content.strip():
                return
            tasks = json.loads(content)

        needs_update = any(task.get('is_running') for task in tasks)

        if needs_update:
            for task in tasks:
                task['is_running'] = False

            async with aiofiles.open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(tasks, ensure_ascii=False, indent=2))
            print("所有任务状态已在配置文件中重置为“已停止”。")

    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"重置任务状态时出错: {e}")


async def reload_scheduler_jobs():
    print("正在重新加载定时任务调度器...")
    scheduler.remove_all_jobs()
    try:
        async with aiofiles.open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            tasks = json.loads(await f.read())

        for i, task in enumerate(tasks):
            task_name = task.get("task_name")
            cron_str = task.get("cron")
            is_enabled = task.get("enabled", False)

            if task_name and cron_str and is_enabled:
                try:
                    trigger = CronTrigger.from_crontab(cron_str)
                    scheduler.add_job(
                        run_single_task,
                        trigger=trigger,
                        args=[i, task_name],
                        id=f"task_{i}",
                        name=f"Scheduled: {task_name}",
                        replace_existing=True
                    )
                    print(f"  -> 已为任务 '{task_name}' 添加定时规则: '{cron_str}'")
                except ValueError as e:
                    print(f"  -> [警告] 任务 '{task_name}' 的 Cron 表达式 '{cron_str}' 无效，已跳过: {e}")

    except FileNotFoundError:
        print(f"[警告] 配置文件 {CONFIG_FILE} 未找到，无法加载定时任务。")
    except Exception as e:
        print(f"[错误] 重新加载定时任务时发生错误: {e}")

    print("定时任务加载完成。")
    if scheduler.get_jobs():
        print("当前已调度的任务:")
        scheduler.print_jobs()


@app.get("/health")
async def health_check():
    return {"status": "healthy", "message": "服务正常运行"}


@app.get("/auth/status")
async def auth_status(username: str = Depends(verify_credentials)):
    return {"authenticated": True, "username": username}


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, username: str = Depends(verify_credentials)):
    return templates.TemplateResponse("index.html", {"request": request})


CONFIG_FILE = "config.json"


@app.get("/api/tasks")
async def get_tasks(username: str = Depends(verify_credentials)):
    try:
        async with aiofiles.open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            content = await f.read()
            tasks = json.loads(content)
            for i, task in enumerate(tasks):
                task['id'] = i
            return tasks
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"配置文件 {CONFIG_FILE} 未找到。")
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail=f"配置文件 {CONFIG_FILE} 格式错误。")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取任务配置时发生错误: {e}")


@app.post("/api/tasks/generate", response_model=dict)
async def generate_task(req: TaskGenerateRequest, username: str = Depends(verify_credentials)):
    req.validate_fields(req)
    if len(req.keyword) > 200:
        raise HTTPException(status_code=400, detail="keyword is too long")
    if len(req.description) > 4000:
        raise HTTPException(status_code=400, detail="description is too long")

    print(f"收到 AI 任务生成请求: {req.task_name}")

    safe_keyword = "".join(c for c in req.keyword.lower().replace(' ', '_') if c.isalnum() or c in "_-").rstrip()
    output_filename = f"prompts/{safe_keyword}_criteria.txt"

    try:
        generated_criteria = await generate_criteria(
            user_description=req.description,
            reference_file_path="prompts/macbook_criteria.txt"
        )
        if not generated_criteria:
            raise HTTPException(status_code=500, detail="AI未能生成分析标准。")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"调用AI生成标准时出错: {e}")

    try:
        os.makedirs("prompts", exist_ok=True)
        async with aiofiles.open(output_filename, 'w', encoding='utf-8') as f:
            await f.write(generated_criteria)
        print(f"新的分析标准已保存到: {output_filename}")
    except IOError as e:
        raise HTTPException(status_code=500, detail=f"保存分析标准文件失败: {e}")

    new_task = {
        "task_name": req.task_name,
        "enabled": True,
        "keyword": req.keyword,
        "max_pages": req.max_pages,
        "personal_only": req.personal_only,
        "min_price": req.min_price,
        "max_price": req.max_price,
        "cron": req.cron,
        "description": req.description,
        "ai_prompt_base_file": "prompts/base_prompt.txt",
        "ai_prompt_criteria_file": output_filename,
        "is_running": False
    }

    success = await update_config_with_new_task(new_task, CONFIG_FILE)
    if not success:
        if os.path.exists(output_filename):
            os.remove(output_filename)
        raise HTTPException(status_code=500, detail="更新配置文件 config.json 失败。")

    await reload_scheduler_jobs()

    async with aiofiles.open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        tasks = json.loads(await f.read())
    new_task_with_id = new_task.copy()
    new_task_with_id['id'] = len(tasks) - 1

    return {"message": "AI 任务创建成功。", "task": new_task_with_id}


@app.post("/api/tasks", response_model=dict)
async def create_task(task: Task, username: str = Depends(verify_credentials)):
    try:
        async with aiofiles.open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            tasks = json.loads(await f.read())
    except (FileNotFoundError, json.JSONDecodeError):
        tasks = []

    new_task_data = task.dict()
    if 'is_running' not in new_task_data:
        new_task_data['is_running'] = False
    tasks.append(new_task_data)

    try:
        async with aiofiles.open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(tasks, ensure_ascii=False, indent=2))

        new_task_data['id'] = len(tasks) - 1
        await reload_scheduler_jobs()
        return {"message": "任务创建成功。", "task": new_task_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入配置文件时发生错误: {e}")


@app.patch("/api/tasks/{task_id}", response_model=dict)
async def update_task_api(task_id: int, task_update: TaskUpdate, username: str = Depends(verify_credentials)):
    task = await get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务未找到。")

    update_data = task_update.dict(exclude_unset=True)

    if not update_data:
        return JSONResponse(content={"message": "数据无变化，未执行更新。"}, status_code=200)

    if 'description' in update_data:
        criteria_filename = task.get('ai_prompt_criteria_file')
        criteria_file_op = FileOperator(criteria_filename)
        try:
            generated_criteria = await generate_criteria(
                user_description=update_data.get("description"),
                reference_file_path="prompts/macbook_criteria.txt"
            )
            if not generated_criteria:
                raise HTTPException(status_code=500, detail="AI未能生成分析标准。")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"调用AI生成标准时出错: {e}")

        success = await criteria_file_op.write(generated_criteria)

        if not success:
            raise HTTPException(status_code=500, detail="更新的AI标准写入出错")

    if 'enabled' in update_data and not update_data['enabled']:
        if scraper_processes.get(task_id):
            print(f"任务 '{task_id}' 已被禁用，正在停止其进程...")
            await stop_task_process(task_id)

    task.update(update_data)

    success = await update_task(task_id, task)

    if not success:
        raise HTTPException(status_code=500, detail="写入配置文件时发生错误")

    return {"message": "任务更新成功。", "task": task}


async def start_task_process(task_id: int, task_name: str):
    global scraper_processes
    if scraper_processes.get(task_id) and scraper_processes[task_id].returncode is None:
        print(f"任务 '{task_name}' (ID: {task_id}) 已在运行中。")
        return

    try:
        os.makedirs("logs", exist_ok=True)
        log_file_path = os.path.join("logs", "scraper.log")
        log_file_handle = open(log_file_path, 'a', encoding='utf-8')

        preexec_fn = os.setsid if sys.platform != "win32" else None
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"

        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "spider_v2.py", "--task-name", task_name,
            stdout=log_file_handle,
            stderr=log_file_handle,
            preexec_fn=preexec_fn,
            env=child_env
        )
        scraper_processes[task_id] = process
        print(f"启动任务 '{task_name}' (PID: {process.pid})，日志输出到 {log_file_path}")
        await update_task_running_status(task_id, True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"启动任务 '{task_name}' 进程时出错: {e}")


async def stop_task_process(task_id: int):
    global scraper_processes
    process = scraper_processes.get(task_id)
    if not process or process.returncode is not None:
        print(f"任务ID {task_id} 没有正在运行的进程。")
        await update_task_running_status(task_id, False)
        if task_id in scraper_processes:
            del scraper_processes[task_id]
        return

    try:
        if sys.platform != "win32":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()

        await process.wait()
        print(f"任务进程 {process.pid} (ID: {task_id}) 已终止。")
    except ProcessLookupError:
        print(f"试图终止的任务进程 (ID: {task_id}) 已不存在。")
    except Exception as e:
        print(f"停止任务进程 (ID: {task_id}) 时出错: {e}")
    finally:
        if task_id in scraper_processes:
            del scraper_processes[task_id]
        await update_task_running_status(task_id, False)


async def update_task_running_status(task_id: int, is_running: bool):
    try:
        async with aiofiles.open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            tasks = json.loads(await f.read())

        if 0 <= task_id < len(tasks):
            tasks[task_id]['is_running'] = is_running
            async with aiofiles.open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(tasks, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"更新任务 {task_id} 状态时出错: {e}")


@app.post("/api/tasks/start/{task_id}", response_model=dict)
async def start_single_task(task_id: int, username: str = Depends(verify_credentials)):
    try:
        async with aiofiles.open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            tasks = json.loads(await f.read())
        if not (0 <= task_id < len(tasks)):
            raise HTTPException(status_code=404, detail="任务未找到。")

        task = tasks[task_id]
        if not task.get("enabled", False):
            raise HTTPException(status_code=400, detail="任务已被禁用，无法启动。")

        await start_task_process(task_id, task['task_name'])
        return {"message": f"任务 '{task['task_name']}' 已启动。"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tasks/stop/{task_id}", response_model=dict)
async def stop_single_task(task_id: int, username: str = Depends(verify_credentials)):
    await stop_task_process(task_id)
    return {"message": f"任务ID {task_id} 已发送停止信号。"}


@app.get("/api/logs")
async def get_logs(from_pos: int = 0, username: str = Depends(verify_credentials)):
    log_file_path = os.path.join("logs", "scraper.log")
    if not os.path.exists(log_file_path):
        return JSONResponse(content={"new_content": "日志文件不存在或尚未创建。", "new_pos": 0})

    try:
        async with aiofiles.open(log_file_path, 'rb') as f:
            await f.seek(0, os.SEEK_END)
            file_size = await f.tell()

        if from_pos >= file_size:
            return {"new_content": "", "new_pos": file_size}

        async with aiofiles.open(log_file_path, 'rb') as f:
            await f.seek(from_pos)
            new_bytes = await f.read()

        new_content = new_bytes.decode('utf-8', errors='replace')
        return {"new_content": new_content, "new_pos": file_size}

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"new_content": f"\n读取日志文件时出错: {e}", "new_pos": from_pos}
        )


@app.delete("/api/logs", response_model=dict)
async def clear_logs(username: str = Depends(verify_credentials)):
    log_file_path = os.path.join("logs", "scraper.log")
    if not os.path.exists(log_file_path):
        return {"message": "日志文件不存在，无需清空。"}

    try:
        async with aiofiles.open(log_file_path, 'w', encoding='utf-8') as f:
            await f.write("")
        return {"message": "日志已成功清空。"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清空日志文件时出错: {e}")


@app.delete("/api/tasks/{task_id}", response_model=dict)
async def delete_task(task_id: int, username: str = Depends(verify_credentials)):
    try:
        async with aiofiles.open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            tasks = json.loads(await f.read())
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail=f"读取或解析配置文件失败: {e}")

    if not (0 <= task_id < len(tasks)):
        raise HTTPException(status_code=404, detail="任务未找到。")

    if scraper_processes.get(task_id):
        await stop_task_process(task_id)

    deleted_task = tasks.pop(task_id)

    criteria_file = deleted_task.get("ai_prompt_criteria_file")
    if criteria_file and os.path.exists(criteria_file):
        try:
            os.remove(criteria_file)
            print(f"成功删除关联的分析标准文件: {criteria_file}")
        except OSError as e:
            print(f"警告: 删除文件 {criteria_file} 失败: {e}")

    try:
        async with aiofiles.open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(tasks, ensure_ascii=False, indent=2))

        await reload_scheduler_jobs()
        return {"message": "任务删除成功。", "task_name": deleted_task.get("task_name")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入配置文件时发生错误: {e}")


@app.get("/api/results/files")
async def list_result_files(username: str = Depends(verify_credentials)):
    jsonl_dir = "jsonl"
    if not os.path.isdir(jsonl_dir):
        return {"files": []}
    files = [f for f in os.listdir(jsonl_dir) if f.endswith(".jsonl")]
    return {"files": files}


@app.delete("/api/results/files/{filename}", response_model=dict)
async def delete_result_file(filename: str, username: str = Depends(verify_credentials)):
    if not filename.endswith(".jsonl") or "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="无效的文件名。")

    filepath = os.path.join("jsonl", filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="结果文件未找到。")

    try:
        os.remove(filepath)
        return {"message": f"结果文件 '{filename}' 已成功删除。"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除结果文件时出错: {e}")


@app.get("/api/results/{filename}")
async def get_result_file_content(filename: str, page: int = 1, limit: int = 20, recommended_only: bool = False, sort_by: str = "crawl_time", sort_order: str = "desc", username: str = Depends(verify_credentials)):
    if not filename.endswith(".jsonl") or "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="无效的文件名。")

    filepath = os.path.join("jsonl", filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="结果文件未找到。")

    results = []
    try:
        async with aiofiles.open(filepath, 'r', encoding='utf-8') as f:
            async for line in f:
                try:
                    record = json.loads(line)
                    if recommended_only:
                        if record.get("ai_analysis", {}).get("is_recommended") is True:
                            results.append(record)
                    else:
                        results.append(record)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取结果文件时出错: {e}")

    def get_sort_key(item):
        info = item.get("商品信息", {})
        if sort_by == "publish_time":
            return info.get("发布时间", "0000-00-00 00:00")
        elif sort_by == "price":
            price_str = str(info.get("当前售价", "0")).replace("¥", "").replace(",", "").strip()
            try:
                return float(price_str)
            except (ValueError, TypeError):
                return 0.0
        else:
            return item.get("爬取时间", "")

    is_reverse = (sort_order == "desc")
    results.sort(key=get_sort_key, reverse=is_reverse)

    total_items = len(results)
    start = (page - 1) * limit
    end = start + limit
    paginated_results = results[start:end]

    return {
        "total_items": total_items,
        "page": page,
        "limit": limit,
        "items": paginated_results
    }


@app.get("/api/settings/status")
async def get_system_status(username: str = Depends(verify_credentials)):
    global scraper_processes
    env_config = dotenv_values(".env")

    running_pids = []
    for task_id, process in list(scraper_processes.items()):
        if process.returncode is None:
            running_pids.append(process.pid)
        else:
            print(f"检测到任务进程 {process.pid} (ID: {task_id}) 已结束，返回码: {process.returncode}。")
            del scraper_processes[task_id]
            asyncio.create_task(update_task_running_status(task_id, False))

    status = {
        "scraper_running": len(running_pids) > 0,
        "login_state_file": {
            "exists": os.path.exists("xianyu_state.json"),
            "path": "xianyu_state.json"
        },
        "env_file": {
            "exists": os.path.exists(".env"),
            "openai_api_key_set": bool(env_config.get("OPENAI_API_KEY")),
            "openai_base_url_set": bool(env_config.get("OPENAI_BASE_URL")),
            "openai_model_name_set": bool(env_config.get("OPENAI_MODEL_NAME")),
            "ntfy_topic_url_set": bool(env_config.get("NTFY_TOPIC_URL")),
            "gotify_url_set": bool(env_config.get("GOTIFY_URL")),
            "gotify_token_set": bool(env_config.get("GOTIFY_TOKEN")),
            "bark_url_set": bool(env_config.get("BARK_URL")),
        }
    }
    return status


PROMPTS_DIR = "prompts"


@app.get("/api/prompts")
async def list_prompts(username: str = Depends(verify_credentials)):
    if not os.path.isdir(PROMPTS_DIR):
        return []
    return [f for f in os.listdir(PROMPTS_DIR) if f.endswith(".txt")]


@app.get("/api/prompts/{filename}")
async def get_prompt_content(filename: str, username: str = Depends(verify_credentials)):
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="无效的文件名。")

    filepath = os.path.join(PROMPTS_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Prompt 文件未找到。")

    async with aiofiles.open(filepath, 'r', encoding='utf-8') as f:
        content = await f.read()
    return {"filename": filename, "content": content}


@app.put("/api/prompts/{filename}")
async def update_prompt_content(filename: str, prompt_update: PromptUpdate, username: str = Depends(verify_credentials)):
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="无效的文件名。")

    filepath = os.path.join(PROMPTS_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Prompt 文件未找到。")

    if len(prompt_update.content) > 200000:
        raise HTTPException(status_code=400, detail="Prompt content is too large")

    try:
        async with aiofiles.open(filepath, 'w', encoding='utf-8') as f:
            await f.write(prompt_update.content)
        return {"message": f"Prompt 文件 '{filename}' 更新成功。"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入 Prompt 文件时出错: {e}")


@app.post("/api/login-state", response_model=dict)
async def update_login_state(data: LoginStateUpdate, username: str = Depends(verify_credentials)):
    data.validate_fields(data)
    state_file = "xianyu_state.json"

    try:
        async with aiofiles.open(state_file, 'w', encoding='utf-8') as f:
            await f.write(data.content)
        os.chmod(state_file, 0o600)
        return {"message": f"登录状态文件 '{state_file}' 已成功更新。"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入登录状态文件时出错: {e}")


@app.delete("/api/login-state", response_model=dict)
async def delete_login_state(username: str = Depends(verify_credentials)):
    state_file = "xianyu_state.json"
    if os.path.exists(state_file):
        try:
            os.remove(state_file)
            return {"message": "登录状态文件已成功删除。"}
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"删除登录状态文件时出错: {e}")
    return {"message": "登录状态文件不存在，无需删除。"}


@app.get("/api/settings/notifications", response_model=dict)
async def get_notification_settings(username: str = Depends(verify_credentials)):
    return load_notification_settings()


@app.put("/api/settings/notifications", response_model=dict)
async def update_notification_settings(settings: NotificationSettings, username: str = Depends(verify_credentials)):
    try:
        settings_dict = settings.dict(exclude_none=True)
        save_notification_settings(settings_dict)
        return {"message": "通知设置已成功更新。"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新通知设置时出错: {e}")


@app.get("/api/settings/ai", response_model=dict)
async def get_ai_settings(username: str = Depends(verify_credentials)):
    return load_ai_settings()


@app.put("/api/settings/ai", response_model=dict)
async def update_ai_settings(settings: dict, username: str = Depends(verify_credentials)):
    try:
        save_ai_settings(settings)
        return {"message": "AI模型设置已成功更新。"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新AI模型设置时出错: {e}")


@app.post("/api/settings/ai/test", response_model=dict)
async def test_ai_settings(settings: dict, username: str = Depends(verify_credentials)):
    try:
        from openai import OpenAI
        import httpx

        client_params = {
            "api_key": settings.get("OPENAI_API_KEY", ""),
            "base_url": settings.get("OPENAI_BASE_URL", ""),
            "timeout": httpx.Timeout(30.0),
        }

        proxy_url = settings.get("PROXY_URL", "")
        if proxy_url:
            client_params["http_client"] = httpx.Client(proxy=proxy_url)

        mode_name = settings.get("OPENAI_MODEL_NAME", "")
        print(f"LOG: 后端容器AI测试 BASE_URL: {client_params['base_url']}, MODEL_NAME: {mode_name}, PROXY_URL: {proxy_url}")

        client = OpenAI(**client_params)

        from src.config import get_ai_request_params

        response = client.chat.completions.create(
            **get_ai_request_params(
                model=mode_name,
                messages=[
                    {"role": "user", "content": "Hello, this is a test message to verify the connection."}
                ],
                max_tokens=10
            )
        )

        return {
            "success": True,
            "message": "AI模型连接测试成功！",
            "response": response.choices[0].message.content if response.choices else "No response"
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"AI模型连接测试失败: {str(e)}"
        }


@app.post("/api/settings/ai/test/backend", response_model=dict)
async def test_ai_settings_backend(username: str = Depends(verify_credentials)):
    try:
        from src.config import client, BASE_URL, MODEL_NAME

        if not client:
            return {
                "success": False,
                "message": "后端AI客户端未初始化，请检查.env配置文件中的AI设置。"
            }

        from src.config import get_ai_request_params

        print(f"LOG: 后端容器AI测试 BASE_URL: {BASE_URL}, MODEL_NAME: {MODEL_NAME}")
        response = await client.chat.completions.create(
            **get_ai_request_params(
                model=MODEL_NAME,
                messages=[
                    {"role": "user", "content": "Hello, this is a test message from backend container to verify connection."}
                ],
                max_tokens=10
            )
        )

        return {
            "success": True,
            "message": "后端AI模型连接测试成功！容器网络正常。",
            "response": response.choices[0].message.content if response.choices else "No response"
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"后端AI模型连接测试失败: {str(e)}。这表明容器内网络可能存在问题。"
        }


if __name__ == "__main__":
    config = dotenv_values(".env")
    server_port = int(config.get("SERVER_PORT", 8000))

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    print(f"启动 Web 管理界面，请在浏览器访问 http://127.0.0.1:{server_port}")
    uvicorn.run(app, host="0.0.0.0", port=server_port)
