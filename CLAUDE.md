# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

团队每日任务管理系统（TaskFlow）— 基于 Flask + SQLite 的轻量级团队任务管理 Web 应用，中文界面，支持多用户协作。

## Commands

```bash
# 开发运行（端口 5001，自动 debug 模式）
source venv/bin/activate
python3 app.py

# 或通过启动脚本
./start.sh

# 生产部署（gunicorn，端口 10000）
gunicorn -c gunicorn_config.py app:app

# 安装依赖
pip install -r requirements.txt
```

默认管理员账号：`admin` / `admin123`

## Architecture

**单文件后端**：所有后端逻辑在 `app.py` 中（约 1500 行），包含数据库初始化、路由、业务逻辑。

**前端**：两个 Jinja2 模板，内联所有 CSS/JS（无构建步骤、无 static 资源）：
- `templates/login.html` — 登录页
- `templates/dashboard.html` — 主界面（约 120KB，包含所有功能面板的完整 SPA）

**数据库**：SQLite（`tasks.db`），WAL 模式，`init_db()` 在启动时自动建表和跑迁移。

### 数据模型

核心表：`users`、`tasks`、`daily_notes`、`comments`、`attachments`、`notifications`、`task_templates`、`subtasks`、`activity_log`、`time_entries`、`task_dependencies`、`tags`、`task_tags`

### 权限模型

两种角色：`admin`（管理员）和 `member`（成员）。
- Admin：可查看所有人任务、指派任务给他人、审核任务、管理用户/模板/标签
- Member：只能看到和操作自己的任务

权限通过 `login_required` 和 `admin_required` 装饰器控制，session 存储 `user_id`、`role`。

### API 结构

所有 API 路径以 `/api/` 开头，返回 JSON。主要模块：
- 认证：`/api/login`、`/api/logout`、`/api/me`
- 任务 CRUD：`/api/tasks`（支持按日期和用户筛选）
- 子任务、评论、附件、标签、依赖：挂在 `/api/tasks/<id>/` 下
- 统计：`/api/stats`、`/api/weekly-stats`、`/api/monthly-stats`、`/api/workload`
- 其他：通知、日报、导出 CSV、任务模板、复制到明天

### 关键行为

- 任务按日期组织（`task_date` 字段），以天为维度管理
- 任务完成时自动创建下一个周期性任务（`recurrence` 字段：daily/weekly/monthly）
- 子任务完成状态自动反算父任务进度
- 评论支持 `@提及` 生成通知
- 密码使用 PBKDF2 哈希，同时兼容旧版 SHA256
- 文件上传保存到 `uploads/` 目录，UUID 重命名

## Environment

- Python 3.14 + Flask 3.1 + Gunicorn
- 无外部数据库依赖，纯 SQLite
- `TASKFLOW_SECRET` 环境变量可覆盖 session secret key
