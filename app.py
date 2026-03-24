#!/usr/bin/env python3
"""团队每日任务管理系统 - Daily Task Manager"""

import sqlite3
import os
import io
import csv
import hashlib
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify, redirect,
    url_for, session, g, make_response
)

app = Flask(__name__)
app.secret_key = os.environ.get('TASKFLOW_SECRET', 'taskflow-secret-key-2026')
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20MB max upload

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'tasks.db')
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

import uuid
from flask import send_from_directory


# ─── Database ───────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            avatar_color TEXT DEFAULT '#4F46E5',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            assigned_to INTEGER NOT NULL,
            created_by INTEGER NOT NULL,
            status TEXT DEFAULT 'todo',
            priority TEXT DEFAULT 'medium',
            progress INTEGER DEFAULT 0,
            due_date DATE,
            task_date DATE NOT NULL,
            category TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY (assigned_to) REFERENCES users(id),
            FOREIGN KEY (created_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS daily_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            note_date DATE NOT NULL,
            content TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            UNIQUE(user_id, note_date)
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_comments_task ON comments(task_id);

        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            file_size INTEGER DEFAULT 0,
            mime_type TEXT DEFAULT '',
            uploaded_by INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (uploaded_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT DEFAULT '',
            task_id INTEGER,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS task_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            priority TEXT DEFAULT 'medium',
            category TEXT DEFAULT '',
            created_by INTEGER NOT NULL,
            FOREIGN KEY (created_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS subtasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            status TEXT DEFAULT 'todo',
            assigned_to INTEGER,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (assigned_to) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            detail TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS time_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            duration INTEGER NOT NULL,
            note TEXT DEFAULT '',
            entry_date DATE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS task_dependencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            depends_on INTEGER NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (depends_on) REFERENCES tasks(id) ON DELETE CASCADE,
            UNIQUE(task_id, depends_on)
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            color TEXT DEFAULT '#6B7280'
        );

        CREATE TABLE IF NOT EXISTS task_tags (
            task_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (task_id, tag_id),
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_date ON tasks(task_date);
        CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks(assigned_to);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_attachments_task ON attachments(task_id);
        CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id);
        CREATE INDEX IF NOT EXISTS idx_subtasks_task ON subtasks(task_id);
        CREATE INDEX IF NOT EXISTS idx_activity_task ON activity_log(task_id);
        CREATE INDEX IF NOT EXISTS idx_time_entries_task ON time_entries(task_id);
    """)

    # Migrations
    migrations = [
        "ALTER TABLE tasks ADD COLUMN review_status TEXT DEFAULT ''",
        "ALTER TABLE tasks ADD COLUMN accepted_at TIMESTAMP",
        "ALTER TABLE tasks ADD COLUMN recurrence TEXT DEFAULT ''",
    ]
    for sql in migrations:
        try:
            db.execute(sql)
            db.commit()
        except sqlite3.OperationalError:
            pass

    # Create default admin if not exists
    pw = hash_password('admin123')
    try:
        db.execute(
            "INSERT INTO users (username, password_hash, display_name, role, avatar_color) "
            "VALUES (?, ?, ?, ?, ?)",
            ('admin', pw, '管理员', 'admin', '#4F46E5')
        )
        db.commit()
    except sqlite3.IntegrityError:
        pass
    db.close()


def hash_password(pw):
    """Use PBKDF2 for new passwords, also accept legacy SHA256."""
    return hashlib.pbkdf2_hmac('sha256', pw.encode(), b'taskflow-salt-2026', 100000).hex()

def verify_password(stored_hash, pw):
    """Verify password against stored hash (supports both PBKDF2 and legacy SHA256)."""
    pbkdf2 = hashlib.pbkdf2_hmac('sha256', pw.encode(), b'taskflow-salt-2026', 100000).hex()
    if stored_hash == pbkdf2:
        return True
    legacy = hashlib.sha256(pw.encode()).hexdigest()
    return stored_hash == legacy


# ─── Auth ───────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': '未登录'}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': '未登录'}), 401
        if session.get('role') != 'admin':
            return jsonify({'error': '需要管理员权限'}), 403
        return f(*args, **kwargs)
    return decorated


# ─── Pages ──────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    return redirect(url_for('dashboard'))


@app.route('/login')
def login_page():
    return render_template('login.html')


@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')


# ─── Auth API ───────────────────────────────────────────────────

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json
    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE username = ?",
        (data['username'],)
    ).fetchone()
    if user and verify_password(user['password_hash'], data['password']):
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['display_name'] = user['display_name']
        session['role'] = user['role']
        return jsonify({'ok': True, 'user': {
            'id': user['id'],
            'username': user['username'],
            'display_name': user['display_name'],
            'role': user['role'],
            'avatar_color': user['avatar_color'],
        }})
    return jsonify({'error': '用户名或密码错误'}), 401


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/me')
@login_required
def api_me():
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],)).fetchone()
    return jsonify({
        'id': user['id'],
        'username': user['username'],
        'display_name': user['display_name'],
        'role': user['role'],
        'avatar_color': user['avatar_color'],
    })


# ─── User Management API ───────────────────────────────────────

@app.route('/api/users', methods=['GET'])
@login_required
def api_get_users():
    db = get_db()
    users = db.execute(
        "SELECT id, username, display_name, role, avatar_color FROM users ORDER BY id"
    ).fetchall()
    return jsonify([dict(u) for u in users])


@app.route('/api/users', methods=['POST'])
@admin_required
def api_create_user():
    data = request.json
    db = get_db()
    colors = ['#4F46E5', '#059669', '#D97706', '#DC2626', '#7C3AED',
              '#2563EB', '#DB2777', '#0891B2', '#65A30D', '#EA580C']
    count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    color = colors[count % len(colors)]
    try:
        db.execute(
            "INSERT INTO users (username, password_hash, display_name, role, avatar_color) "
            "VALUES (?, ?, ?, ?, ?)",
            (data['username'], hash_password(data.get('password', '123456')),
             data['display_name'], data.get('role', 'member'), color)
        )
        db.commit()
        return jsonify({'ok': True})
    except sqlite3.IntegrityError:
        return jsonify({'error': '用户名已存在'}), 400


@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@admin_required
def api_delete_user(user_id):
    if user_id == session['user_id']:
        return jsonify({'error': '不能删除自己'}), 400
    db = get_db()
    db.execute("DELETE FROM tasks WHERE assigned_to = ? OR created_by = ?", (user_id, user_id))
    db.execute("DELETE FROM daily_notes WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/users/<int:user_id>/reset-password', methods=['POST'])
@admin_required
def api_reset_password(user_id):
    db = get_db()
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password('123456'), user_id)
    )
    db.commit()
    return jsonify({'ok': True, 'message': '密码已重置为 123456'})


@app.route('/api/change-password', methods=['POST'])
@login_required
def api_change_password():
    data = request.json
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],)).fetchone()
    if not verify_password(user['password_hash'], data['old_password']):
        return jsonify({'error': '旧密码错误'}), 400
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(data['new_password']), session['user_id'])
    )
    db.commit()
    return jsonify({'ok': True})


# ─── Tasks API ──────────────────────────────────────────────────

@app.route('/api/tasks', methods=['GET'])
@login_required
def api_get_tasks():
    db = get_db()
    task_date = request.args.get('date', date.today().isoformat())
    user_id = request.args.get('user_id', '').strip()

    query = """
        SELECT t.*, u1.display_name as assigned_name, u1.avatar_color,
               u2.display_name as creator_name,
               (SELECT COUNT(*) FROM attachments WHERE task_id = t.id) as attach_count,
               (SELECT COUNT(*) FROM comments WHERE task_id = t.id) as comment_count,
               (SELECT COUNT(*) FROM notifications WHERE task_id = t.id AND user_id = t.assigned_to AND is_read = 0) as has_unread
        FROM tasks t
        JOIN users u1 ON t.assigned_to = u1.id
        JOIN users u2 ON t.created_by = u2.id
        WHERE t.task_date = ?
    """
    params = [task_date]

    if user_id:
        query += " AND t.assigned_to = ?"
        params.append(int(user_id))
    elif session.get('role') != 'admin':
        query += " AND t.assigned_to = ?"
        params.append(session['user_id'])

    query += " ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, t.created_at"

    tasks = db.execute(query, params).fetchall()
    return jsonify([dict(t) for t in tasks])


@app.route('/api/tasks', methods=['POST'])
@login_required
def api_create_task():
    data = request.json
    db = get_db()

    # Admin can assign to anyone, members can only create for themselves
    assigned_to = data.get('assigned_to', session['user_id'])
    if session.get('role') != 'admin' and int(assigned_to) != session['user_id']:
        return jsonify({'error': '只能给自己创建任务'}), 403

    cursor = db.execute(
        """INSERT INTO tasks (title, description, assigned_to, created_by,
           status, priority, progress, due_date, task_date, category, recurrence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (data['title'], data.get('description', ''),
         assigned_to, session['user_id'],
         data.get('status', 'todo'), data.get('priority', 'medium'),
         data.get('progress', 0), data.get('due_date'),
         data.get('task_date', date.today().isoformat()),
         data.get('category', ''), data.get('recurrence', ''))
    )
    task_id = cursor.lastrowid
    log_activity(db, task_id, session['user_id'], '创建任务', data['title'])
    # Notify assigned employee
    if assigned_to != session['user_id']:
        create_notification(db, assigned_to, 'new_task',
            '收到新任务', data['title'], task_id)
    db.commit()
    return jsonify({'ok': True, 'task_id': task_id})


@app.route('/api/tasks/<int:task_id>', methods=['PUT'])
@login_required
def api_update_task(task_id):
    data = request.json
    db = get_db()

    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    # Members can only update their own tasks
    if session.get('role') != 'admin' and task['assigned_to'] != session['user_id']:
        return jsonify({'error': '无权修改此任务'}), 403

    completed_at = task['completed_at']
    status = data.get('status', task['status'])
    progress = data.get('progress', task['progress'])

    if status == 'done' and task['status'] != 'done':
        completed_at = datetime.now().isoformat()
        progress = 100
    elif status != 'done':
        completed_at = None

    # Log changes
    status_labels = {'todo':'待办','in_progress':'进行中','done':'已完成'}
    if status != task['status']:
        log_activity(db, task_id, session['user_id'], '状态变更',
            f'{status_labels.get(task["status"],task["status"])} → {status_labels.get(status,status)}')
    if data.get('priority') and data['priority'] != task['priority']:
        log_activity(db, task_id, session['user_id'], '优先级变更',
            f'{task["priority"]} → {data["priority"]}')
    if data.get('assigned_to') and int(data['assigned_to']) != task['assigned_to']:
        log_activity(db, task_id, session['user_id'], '重新指派', '')
    if progress != task['progress']:
        log_activity(db, task_id, session['user_id'], '进度更新', f'{task["progress"]}% → {progress}%')

    db.execute(
        """UPDATE tasks SET title=?, description=?, status=?, priority=?,
           progress=?, due_date=?, category=?, updated_at=?, completed_at=?,
           assigned_to=?
           WHERE id=?""",
        (data.get('title', task['title']),
         data.get('description', task['description']),
         status, data.get('priority', task['priority']),
         progress, data.get('due_date', task['due_date']),
         data.get('category', task['category']),
         datetime.now().isoformat(), completed_at,
         data.get('assigned_to', task['assigned_to']),
         task_id)
    )

    # Auto-create next recurring task when completed
    if status == 'done' and task['status'] != 'done':
        recurrence = task['recurrence'] if 'recurrence' in task.keys() else ''
        if recurrence:
            next_date = None
            today_d = date.today()
            if recurrence == 'daily':
                next_date = today_d + timedelta(days=1)
            elif recurrence == 'weekly':
                next_date = today_d + timedelta(days=7)
            elif recurrence == 'monthly':
                next_date = today_d.replace(month=today_d.month+1) if today_d.month < 12 else today_d.replace(year=today_d.year+1, month=1)
            if next_date:
                db.execute(
                    """INSERT INTO tasks (title, description, assigned_to, created_by,
                       status, priority, due_date, task_date, category, recurrence)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (task['title'], task['description'], task['assigned_to'], task['created_by'],
                     'todo', task['priority'], None, next_date.isoformat(),
                     task['category'], recurrence)
                )

    db.commit()
    return jsonify({'ok': True})


@app.route('/api/tasks/<int:task_id>', methods=['DELETE'])
@login_required
def api_delete_task(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if session.get('role') != 'admin' and task['created_by'] != session['user_id']:
        return jsonify({'error': '无权删除此任务'}), 403
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    db.commit()
    return jsonify({'ok': True})


# ─── Dashboard Stats API ───────────────────────────────────────

@app.route('/api/stats')
@login_required
def api_stats():
    db = get_db()
    today = date.today().isoformat()

    # Per-user stats for today — employees only see themselves
    if session.get('role') == 'admin':
        users = db.execute(
            "SELECT id, display_name, avatar_color FROM users ORDER BY id"
        ).fetchall()
    else:
        users = db.execute(
            "SELECT id, display_name, avatar_color FROM users WHERE id = ?",
            (session['user_id'],)
        ).fetchall()

    user_stats = []
    for u in users:
        tasks = db.execute(
            """SELECT title, status, progress, priority, review_status
               FROM tasks WHERE assigned_to = ? AND task_date = ?
               ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2 ELSE 3 END""",
            (u['id'], today)
        ).fetchall()
        task_list = [dict(t) for t in tasks]
        total = len(task_list)
        done = sum(1 for t in task_list if t['status'] == 'done')
        in_progress = sum(1 for t in task_list if t['status'] == 'in_progress')
        avg_progress = (sum(t['progress'] for t in task_list) / total) if total > 0 else 0

        user_stats.append({
            'user_id': u['id'],
            'display_name': u['display_name'],
            'avatar_color': u['avatar_color'],
            'total': total,
            'done': done,
            'in_progress': in_progress,
            'todo': total - done - in_progress,
            'avg_progress': round(avg_progress),
            'tasks': task_list,
        })

    # Overall
    all_tasks = db.execute(
        "SELECT status FROM tasks WHERE task_date = ?", (today,)
    ).fetchall()
    total_all = len(all_tasks)
    done_all = sum(1 for t in all_tasks if t['status'] == 'done')

    return jsonify({
        'date': today,
        'total_tasks': total_all,
        'completed_tasks': done_all,
        'completion_rate': round((done_all / total_all * 100) if total_all > 0 else 0),
        'user_stats': user_stats,
    })


@app.route('/api/weekly-stats')
@login_required
def api_weekly_stats():
    db = get_db()
    today = date.today()
    week_start = today - timedelta(days=today.weekday())

    days = []
    for i in range(7):
        d = (week_start + timedelta(days=i)).isoformat()
        tasks = db.execute(
            "SELECT status FROM tasks WHERE task_date = ?", (d,)
        ).fetchall()
        total = len(tasks)
        done = sum(1 for t in tasks if t['status'] == 'done')
        days.append({
            'date': d,
            'weekday': ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][i],
            'total': total,
            'done': done,
        })

    return jsonify(days)


# ─── Daily Notes API ───────────────────────────────────────────

@app.route('/api/notes', methods=['GET'])
@login_required
def api_get_notes():
    db = get_db()
    note_date = request.args.get('date', date.today().isoformat())
    user_id = request.args.get('user_id', session['user_id'])

    if session.get('role') != 'admin' and int(user_id) != session['user_id']:
        return jsonify({'error': '无权查看'}), 403

    note = db.execute(
        "SELECT * FROM daily_notes WHERE user_id = ? AND note_date = ?",
        (user_id, note_date)
    ).fetchone()
    return jsonify(dict(note) if note else {'content': ''})


@app.route('/api/notes', methods=['POST'])
@login_required
def api_save_note():
    data = request.json
    db = get_db()
    db.execute(
        """INSERT INTO daily_notes (user_id, note_date, content, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id, note_date) DO UPDATE SET content=?, updated_at=?""",
        (session['user_id'], data.get('date', date.today().isoformat()),
         data['content'], datetime.now().isoformat(),
         data['content'], datetime.now().isoformat())
    )
    db.commit()
    return jsonify({'ok': True})


# ─── Comments API ─────────────────────────────────────────────

@app.route('/api/tasks/<int:task_id>/comments', methods=['GET'])
@login_required
def api_get_comments(task_id):
    db = get_db()
    comments = db.execute(
        """SELECT c.*, u.display_name, u.avatar_color, u.role as user_role
           FROM comments c JOIN users u ON c.user_id = u.id
           WHERE c.task_id = ? ORDER BY c.created_at ASC""",
        (task_id,)
    ).fetchall()
    return jsonify([dict(c) for c in comments])


@app.route('/api/tasks/<int:task_id>/comments', methods=['POST'])
@login_required
def api_add_comment(task_id):
    data = request.json
    content = data.get('content', '').strip()
    if not content:
        return jsonify({'error': '评论不能为空'}), 400
    db = get_db()
    db.execute(
        "INSERT INTO comments (task_id, user_id, content) VALUES (?, ?, ?)",
        (task_id, session['user_id'], content)
    )
    log_activity(db, task_id, session['user_id'], '发表评论', content[:50])
    # Handle @mentions
    import re
    mentions = re.findall(r'@(\S+)', content)
    if mentions:
        users = db.execute("SELECT id, display_name FROM users").fetchall()
        name_map = {u['display_name']: u['id'] for u in users}
        for mention in mentions:
            uid = name_map.get(mention)
            if uid and uid != session['user_id']:
                create_notification(db, uid, 'mention',
                    f'{session["display_name"]} 在评论中提到了你',
                    content[:80], task_id)
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/comments/<int:comment_id>', methods=['DELETE'])
@login_required
def api_delete_comment(comment_id):
    db = get_db()
    comment = db.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
    if not comment:
        return jsonify({'error': '评论不存在'}), 404
    if session.get('role') != 'admin' and comment['user_id'] != session['user_id']:
        return jsonify({'error': '无权删除'}), 403
    db.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    db.commit()
    return jsonify({'ok': True})


# ─── Review API ───────────────────────────────────────────────

@app.route('/api/tasks/<int:task_id>/review', methods=['POST'])
@admin_required
def api_review_task(task_id):
    data = request.json
    review_status = data.get('review_status', '')  # approved / needs_revision / ''
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    db.execute(
        "UPDATE tasks SET review_status = ?, updated_at = ? WHERE id = ?",
        (review_status, datetime.now().isoformat(), task_id)
    )
    db.commit()
    return jsonify({'ok': True})


# ─── Attachments API ──────────────────────────────────────────

@app.route('/api/tasks/<int:task_id>/attachments', methods=['GET'])
@login_required
def api_get_attachments(task_id):
    db = get_db()
    attachments = db.execute(
        """SELECT a.*, u.display_name as uploader_name
           FROM attachments a JOIN users u ON a.uploaded_by = u.id
           WHERE a.task_id = ? ORDER BY a.created_at""",
        (task_id,)
    ).fetchall()
    return jsonify([dict(a) for a in attachments])


@app.route('/api/tasks/<int:task_id>/attachments', methods=['POST'])
@login_required
def api_upload_attachment(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    if 'file' not in request.files:
        return jsonify({'error': '没有文件'}), 400

    uploaded = []
    for f in request.files.getlist('file'):
        if not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        safe_name = str(uuid.uuid4()) + ext
        filepath = os.path.join(UPLOAD_DIR, safe_name)
        f.save(filepath)
        file_size = os.path.getsize(filepath)

        db.execute(
            """INSERT INTO attachments (task_id, filename, original_name, file_size, mime_type, uploaded_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, safe_name, f.filename, file_size, f.content_type or '', session['user_id'])
        )
        uploaded.append(f.filename)

    db.commit()
    return jsonify({'ok': True, 'uploaded': uploaded})


@app.route('/uploads/<filename>')
@login_required
def serve_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route('/api/attachments/<int:att_id>', methods=['DELETE'])
@login_required
def api_delete_attachment(att_id):
    db = get_db()
    att = db.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
    if not att:
        return jsonify({'error': '附件不存在'}), 404
    if session.get('role') != 'admin' and att['uploaded_by'] != session['user_id']:
        return jsonify({'error': '无权删除'}), 403

    filepath = os.path.join(UPLOAD_DIR, att['filename'])
    if os.path.exists(filepath):
        os.remove(filepath)
    db.execute("DELETE FROM attachments WHERE id = ?", (att_id,))
    db.commit()
    return jsonify({'ok': True})


# ─── Notifications ────────────────────────────────────────────

def log_activity(db, task_id, user_id, action, detail=''):
    db.execute(
        "INSERT INTO activity_log (task_id, user_id, action, detail) VALUES (?,?,?,?)",
        (task_id, user_id, action, detail)
    )

def create_notification(db, user_id, ntype, title, content='', task_id=None):
    db.execute(
        "INSERT INTO notifications (user_id, type, title, content, task_id) VALUES (?,?,?,?,?)",
        (user_id, ntype, title, content, task_id)
    )


@app.route('/api/notifications')
@login_required
def api_get_notifications():
    db = get_db()
    notes = db.execute(
        """SELECT * FROM notifications WHERE user_id = ?
           ORDER BY created_at DESC LIMIT 50""",
        (session['user_id'],)
    ).fetchall()
    unread = db.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
        (session['user_id'],)
    ).fetchone()[0]
    return jsonify({'notifications': [dict(n) for n in notes], 'unread': unread})


@app.route('/api/notifications/read', methods=['POST'])
@login_required
def api_read_notifications():
    db = get_db()
    db.execute(
        "UPDATE notifications SET is_read = 1 WHERE user_id = ?",
        (session['user_id'],)
    )
    db.commit()
    return jsonify({'ok': True})


# ─── Subtasks API ────────────────────────────────────────────

@app.route('/api/tasks/<int:task_id>/subtasks', methods=['GET'])
@login_required
def api_get_subtasks(task_id):
    db = get_db()
    subs = db.execute(
        """SELECT s.*, u.display_name as assigned_name
           FROM subtasks s LEFT JOIN users u ON s.assigned_to = u.id
           WHERE s.task_id = ? ORDER BY s.sort_order, s.id""",
        (task_id,)
    ).fetchall()
    return jsonify([dict(s) for s in subs])


@app.route('/api/tasks/<int:task_id>/subtasks', methods=['POST'])
@login_required
def api_add_subtask(task_id):
    data = request.json
    db = get_db()
    db.execute(
        "INSERT INTO subtasks (task_id, title, assigned_to, sort_order) VALUES (?,?,?,?)",
        (task_id, data['title'], data.get('assigned_to'), data.get('sort_order', 0))
    )
    log_activity(db, task_id, session['user_id'], '添加子任务', data['title'])
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/subtasks/<int:sub_id>', methods=['PUT'])
@login_required
def api_update_subtask(sub_id):
    data = request.json
    db = get_db()
    sub = db.execute("SELECT * FROM subtasks WHERE id = ?", (sub_id,)).fetchone()
    if not sub:
        return jsonify({'error': '子任务不存在'}), 404
    old_status = sub['status']
    new_status = data.get('status', sub['status'])
    db.execute(
        "UPDATE subtasks SET title=?, status=?, assigned_to=? WHERE id=?",
        (data.get('title', sub['title']), new_status,
         data.get('assigned_to', sub['assigned_to']), sub_id)
    )
    if old_status != new_status:
        log_activity(db, sub['task_id'], session['user_id'], '子任务状态变更',
            f'{sub["title"]}: {old_status} → {new_status}')
    # Auto-update parent progress based on subtask completion
    all_subs = db.execute("SELECT status FROM subtasks WHERE task_id = ?", (sub['task_id'],)).fetchall()
    if all_subs:
        done_count = sum(1 for s in all_subs if s['status'] == 'done')
        progress = round(done_count / len(all_subs) * 100)
        db.execute("UPDATE tasks SET progress = ? WHERE id = ?", (progress, sub['task_id']))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/subtasks/<int:sub_id>', methods=['DELETE'])
@login_required
def api_delete_subtask(sub_id):
    db = get_db()
    sub = db.execute("SELECT * FROM subtasks WHERE id = ?", (sub_id,)).fetchone()
    if not sub:
        return jsonify({'error': '子任务不存在'}), 404
    db.execute("DELETE FROM subtasks WHERE id = ?", (sub_id,))
    db.commit()
    return jsonify({'ok': True})


# ─── Activity Log API ────────────────────────────────────────

@app.route('/api/tasks/<int:task_id>/activity')
@login_required
def api_get_activity(task_id):
    db = get_db()
    logs = db.execute(
        """SELECT a.*, u.display_name, u.avatar_color
           FROM activity_log a JOIN users u ON a.user_id = u.id
           WHERE a.task_id = ? ORDER BY a.created_at DESC LIMIT 50""",
        (task_id,)
    ).fetchall()
    return jsonify([dict(l) for l in logs])


# ─── Accept Task API ─────────────────────────────────────────

@app.route('/api/tasks/<int:task_id>/accept', methods=['POST'])
@login_required
def api_accept_task(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['assigned_to'] != session['user_id']:
        return jsonify({'error': '只能接受分配给自己的任务'}), 403
    db.execute(
        "UPDATE tasks SET accepted_at = ?, updated_at = ? WHERE id = ?",
        (datetime.now().isoformat(), datetime.now().isoformat(), task_id)
    )
    db.commit()
    # Notify admin
    admins = db.execute("SELECT id FROM users WHERE role = 'admin'").fetchall()
    for a in admins:
        create_notification(db, a['id'], 'task_accepted',
            f'{session["display_name"]} 已接受任务',
            task['title'], task_id)
    db.commit()
    return jsonify({'ok': True})


# ─── Task Templates API ──────────────────────────────────────

@app.route('/api/templates', methods=['GET'])
@login_required
def api_get_templates():
    db = get_db()
    templates = db.execute("SELECT * FROM task_templates ORDER BY name").fetchall()
    return jsonify([dict(t) for t in templates])


@app.route('/api/templates', methods=['POST'])
@admin_required
def api_create_template():
    data = request.json
    db = get_db()
    db.execute(
        "INSERT INTO task_templates (name, title, description, priority, category, created_by) VALUES (?,?,?,?,?,?)",
        (data['name'], data['title'], data.get('description', ''),
         data.get('priority', 'medium'), data.get('category', ''), session['user_id'])
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/templates/<int:tpl_id>', methods=['DELETE'])
@admin_required
def api_delete_template(tpl_id):
    db = get_db()
    db.execute("DELETE FROM task_templates WHERE id = ?", (tpl_id,))
    db.commit()
    return jsonify({'ok': True})


# ─── Monthly Stats API ───────────────────────────────────────

@app.route('/api/monthly-stats')
@login_required
def api_monthly_stats():
    db = get_db()
    year = request.args.get('year', date.today().year)
    month = request.args.get('month', date.today().month)
    month_start = f"{year}-{int(month):02d}-01"
    if int(month) == 12:
        month_end = f"{int(year)+1}-01-01"
    else:
        month_end = f"{year}-{int(month)+1:02d}-01"

    users = db.execute("SELECT id, display_name, avatar_color FROM users ORDER BY id").fetchall()
    user_stats = []
    for u in users:
        tasks = db.execute(
            "SELECT status FROM tasks WHERE assigned_to = ? AND task_date >= ? AND task_date < ?",
            (u['id'], month_start, month_end)
        ).fetchall()
        total = len(tasks)
        done = sum(1 for t in tasks if t['status'] == 'done')
        user_stats.append({
            'user_id': u['id'],
            'display_name': u['display_name'],
            'avatar_color': u['avatar_color'],
            'total': total,
            'done': done,
            'rate': round((done / total * 100) if total > 0 else 0),
        })

    # Weekly breakdown
    weeks = []
    current = date.fromisoformat(month_start)
    end = date.fromisoformat(month_end)
    week_num = 1
    while current < end:
        week_end = min(current + timedelta(days=6-current.weekday()), end - timedelta(days=1))
        tasks = db.execute(
            "SELECT status FROM tasks WHERE task_date >= ? AND task_date <= ?",
            (current.isoformat(), week_end.isoformat())
        ).fetchall()
        total = len(tasks)
        done = sum(1 for t in tasks if t['status'] == 'done')
        weeks.append({
            'week': f'第{week_num}周',
            'start': current.isoformat(),
            'end': week_end.isoformat(),
            'total': total,
            'done': done,
            'rate': round((done / total * 100) if total > 0 else 0),
        })
        current = week_end + timedelta(days=1)
        week_num += 1

    return jsonify({'user_stats': user_stats, 'weeks': weeks})


# ─── Time Tracking API ───────────────────────────────────────

@app.route('/api/tasks/<int:task_id>/time', methods=['GET'])
@login_required
def api_get_time_entries(task_id):
    db = get_db()
    entries = db.execute(
        """SELECT te.*, u.display_name FROM time_entries te
           JOIN users u ON te.user_id = u.id
           WHERE te.task_id = ? ORDER BY te.entry_date DESC""",
        (task_id,)
    ).fetchall()
    total = db.execute(
        "SELECT COALESCE(SUM(duration),0) FROM time_entries WHERE task_id = ?",
        (task_id,)
    ).fetchone()[0]
    return jsonify({'entries': [dict(e) for e in entries], 'total_minutes': total})


@app.route('/api/tasks/<int:task_id>/time', methods=['POST'])
@login_required
def api_add_time_entry(task_id):
    data = request.json
    db = get_db()
    db.execute(
        "INSERT INTO time_entries (task_id, user_id, duration, note, entry_date) VALUES (?,?,?,?,?)",
        (task_id, session['user_id'], data['duration'],
         data.get('note', ''), data.get('date', date.today().isoformat()))
    )
    log_activity(db, task_id, session['user_id'], '记录工时', f'{data["duration"]}分钟')
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/time-entries/<int:entry_id>', methods=['DELETE'])
@login_required
def api_delete_time_entry(entry_id):
    db = get_db()
    entry = db.execute("SELECT * FROM time_entries WHERE id = ?", (entry_id,)).fetchone()
    if not entry:
        return jsonify({'error': '不存在'}), 404
    if session.get('role') != 'admin' and entry['user_id'] != session['user_id']:
        return jsonify({'error': '无权删除'}), 403
    db.execute("DELETE FROM time_entries WHERE id = ?", (entry_id,))
    db.commit()
    return jsonify({'ok': True})


# ─── Task Dependencies API ───────────────────────────────────

@app.route('/api/tasks/<int:task_id>/dependencies', methods=['GET'])
@login_required
def api_get_dependencies(task_id):
    db = get_db()
    deps = db.execute(
        """SELECT td.id, td.depends_on, t.title, t.status
           FROM task_dependencies td JOIN tasks t ON td.depends_on = t.id
           WHERE td.task_id = ?""",
        (task_id,)
    ).fetchall()
    return jsonify([dict(d) for d in deps])


@app.route('/api/tasks/<int:task_id>/dependencies', methods=['POST'])
@login_required
def api_add_dependency(task_id):
    data = request.json
    db = get_db()
    try:
        db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on) VALUES (?,?)",
            (task_id, data['depends_on'])
        )
        db.commit()
        return jsonify({'ok': True})
    except sqlite3.IntegrityError:
        return jsonify({'error': '依赖已存在'}), 400


@app.route('/api/dependencies/<int:dep_id>', methods=['DELETE'])
@login_required
def api_delete_dependency(dep_id):
    db = get_db()
    db.execute("DELETE FROM task_dependencies WHERE id = ?", (dep_id,))
    db.commit()
    return jsonify({'ok': True})


# ─── Tags API ────────────────────────────────────────────────

@app.route('/api/tags', methods=['GET'])
@login_required
def api_get_tags():
    db = get_db()
    tags = db.execute("SELECT * FROM tags ORDER BY name").fetchall()
    return jsonify([dict(t) for t in tags])


@app.route('/api/tags', methods=['POST'])
@login_required
def api_create_tag():
    data = request.json
    db = get_db()
    try:
        db.execute("INSERT INTO tags (name, color) VALUES (?,?)",
                   (data['name'], data.get('color', '#6B7280')))
        db.commit()
        return jsonify({'ok': True})
    except sqlite3.IntegrityError:
        return jsonify({'error': '标签已存在'}), 400


@app.route('/api/tags/<int:tag_id>', methods=['DELETE'])
@admin_required
def api_delete_tag(tag_id):
    db = get_db()
    db.execute("DELETE FROM task_tags WHERE tag_id = ?", (tag_id,))
    db.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/tasks/<int:task_id>/tags', methods=['GET'])
@login_required
def api_get_task_tags(task_id):
    db = get_db()
    tags = db.execute(
        """SELECT t.* FROM tags t JOIN task_tags tt ON t.id = tt.tag_id
           WHERE tt.task_id = ?""",
        (task_id,)
    ).fetchall()
    return jsonify([dict(t) for t in tags])


@app.route('/api/tasks/<int:task_id>/tags', methods=['POST'])
@login_required
def api_set_task_tags(task_id):
    data = request.json
    db = get_db()
    db.execute("DELETE FROM task_tags WHERE task_id = ?", (task_id,))
    for tag_id in data.get('tag_ids', []):
        db.execute("INSERT OR IGNORE INTO task_tags (task_id, tag_id) VALUES (?,?)",
                   (task_id, tag_id))
    db.commit()
    return jsonify({'ok': True})


# ─── Calendar API ────────────────────────────────────────────

@app.route('/api/calendar')
@login_required
def api_calendar():
    db = get_db()
    start = request.args.get('start')
    end = request.args.get('end')
    user_id = request.args.get('user_id', '').strip()

    query = """
        SELECT t.id, t.title, t.task_date, t.due_date, t.status, t.priority,
               t.progress, t.assigned_to, u.display_name as assigned_name, u.avatar_color
        FROM tasks t JOIN users u ON t.assigned_to = u.id
        WHERE t.task_date BETWEEN ? AND ?
    """
    params = [start, end]
    if user_id:
        query += " AND t.assigned_to = ?"
        params.append(int(user_id))
    elif session.get('role') != 'admin':
        query += " AND t.assigned_to = ?"
        params.append(session['user_id'])
    query += " ORDER BY t.task_date, t.priority"
    tasks = db.execute(query, params).fetchall()
    return jsonify([dict(t) for t in tasks])


# ─── Workload API ────────────────────────────────────────────

@app.route('/api/workload')
@login_required
def api_workload():
    db = get_db()
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)

    users = db.execute("SELECT id, display_name, avatar_color FROM users ORDER BY id").fetchall()
    result = []
    for u in users:
        tasks = db.execute(
            """SELECT status, priority FROM tasks
               WHERE assigned_to = ? AND task_date BETWEEN ? AND ?""",
            (u['id'], week_start.isoformat(), week_end.isoformat())
        ).fetchall()
        total = len(tasks)
        done = sum(1 for t in tasks if t['status'] == 'done')
        urgent = sum(1 for t in tasks if t['priority'] in ('urgent', 'high'))
        hours = db.execute(
            """SELECT COALESCE(SUM(duration),0) FROM time_entries
               WHERE user_id = ? AND entry_date BETWEEN ? AND ?""",
            (u['id'], week_start.isoformat(), week_end.isoformat())
        ).fetchone()[0]
        result.append({
            'user_id': u['id'],
            'display_name': u['display_name'],
            'avatar_color': u['avatar_color'],
            'total_tasks': total,
            'done': done,
            'pending': total - done,
            'urgent_count': urgent,
            'hours_logged': round(hours / 60, 1),
            'load_level': 'heavy' if total - done > 8 else 'normal' if total - done > 3 else 'light',
        })
    return jsonify(result)


# ─── Copy Tasks to Tomorrow ────────────────────────────────────

@app.route('/api/tasks/copy-to-tomorrow', methods=['POST'])
@login_required
def api_copy_to_tomorrow():
    """Copy incomplete tasks from a given date to the next day."""
    data = request.json
    from_date = data.get('from_date', date.today().isoformat())
    to_date_obj = date.fromisoformat(from_date) + timedelta(days=1)
    to_date = to_date_obj.isoformat()
    db = get_db()

    # Get incomplete tasks
    query = """
        SELECT * FROM tasks
        WHERE task_date = ? AND status != 'done'
    """
    params = [from_date]

    # Non-admin can only copy their own
    if session.get('role') != 'admin':
        query += " AND assigned_to = ?"
        params.append(session['user_id'])

    tasks = db.execute(query, params).fetchall()
    copied = 0
    for t in tasks:
        # Check if already exists tomorrow (avoid duplicates)
        existing = db.execute(
            "SELECT id FROM tasks WHERE title = ? AND assigned_to = ? AND task_date = ?",
            (t['title'], t['assigned_to'], to_date)
        ).fetchone()
        if not existing:
            db.execute(
                """INSERT INTO tasks (title, description, assigned_to, created_by,
                   status, priority, progress, due_date, task_date, category)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (t['title'], t['description'], t['assigned_to'], t['created_by'],
                 t['status'], t['priority'], t['progress'],
                 t['due_date'], to_date, t['category'])
            )
            copied += 1
    db.commit()
    return jsonify({'ok': True, 'copied': copied, 'to_date': to_date})


# ─── Daily Report API ─────────────────────────────────────────

@app.route('/api/daily-report')
@login_required
def api_daily_report():
    """Generate a daily report for a given date."""
    db = get_db()
    report_date = request.args.get('date', date.today().isoformat())

    users = db.execute(
        "SELECT id, display_name, avatar_color FROM users ORDER BY id"
    ).fetchall()

    report = []
    total_all = 0
    done_all = 0

    for u in users:
        tasks = db.execute(
            """SELECT title, status, priority, progress, category, completed_at,
                      description
               FROM tasks WHERE assigned_to = ? AND task_date = ?
               ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2 ELSE 3 END""",
            (u['id'], report_date)
        ).fetchall()

        user_tasks = [dict(t) for t in tasks]
        total = len(user_tasks)
        done = sum(1 for t in user_tasks if t['status'] == 'done')
        in_progress = sum(1 for t in user_tasks if t['status'] == 'in_progress')
        avg_progress = round(sum(t['progress'] for t in user_tasks) / total) if total > 0 else 0

        total_all += total
        done_all += done

        report.append({
            'user_id': u['id'],
            'display_name': u['display_name'],
            'avatar_color': u['avatar_color'],
            'total': total,
            'done': done,
            'in_progress': in_progress,
            'todo': total - done - in_progress,
            'avg_progress': avg_progress,
            'tasks': user_tasks,
        })

    # Find overdue tasks
    overdue = db.execute(
        """SELECT t.title, t.due_date, t.status, t.priority, t.assigned_to,
                  u.display_name as assigned_name
           FROM tasks t JOIN users u ON t.assigned_to = u.id
           WHERE t.due_date < ? AND t.status != 'done'
           ORDER BY t.due_date""",
        (report_date,)
    ).fetchall()

    return jsonify({
        'date': report_date,
        'total': total_all,
        'done': done_all,
        'completion_rate': round((done_all / total_all * 100) if total_all > 0 else 0),
        'users': report,
        'overdue': [dict(o) for o in overdue],
    })


# ─── Export Excel/CSV API ─────────────────────────────────────

@app.route('/api/export')
@login_required
def api_export():
    """Export tasks as CSV for a date range."""
    db = get_db()
    start_date = request.args.get('start', date.today().isoformat())
    end_date = request.args.get('end', date.today().isoformat())
    export_format = request.args.get('format', 'csv')

    tasks = db.execute(
        """SELECT t.task_date, u1.display_name as assigned_name,
                  t.title, t.description, t.status, t.priority,
                  t.progress, t.category, t.due_date, t.completed_at,
                  u2.display_name as creator_name
           FROM tasks t
           JOIN users u1 ON t.assigned_to = u1.id
           JOIN users u2 ON t.created_by = u2.id
           WHERE t.task_date BETWEEN ? AND ?
           ORDER BY t.task_date, u1.display_name, t.priority""",
        (start_date, end_date)
    ).fetchall()

    status_map = {'todo': '待办', 'in_progress': '进行中', 'done': '已完成'}
    priority_map = {'urgent': '紧急', 'high': '高', 'medium': '中', 'low': '低'}

    output = io.StringIO()
    # Write BOM for Excel to recognize UTF-8
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow(['日期', '负责人', '任务标题', '描述', '状态', '优先级',
                     '进度%', '分类', '截止日期', '完成时间', '创建人'])
    for t in tasks:
        writer.writerow([
            t['task_date'], t['assigned_name'], t['title'],
            t['description'] or '', status_map.get(t['status'], t['status']),
            priority_map.get(t['priority'], t['priority']),
            t['progress'], t['category'] or '',
            t['due_date'] or '', t['completed_at'] or '',
            t['creator_name']
        ])

    response = make_response(output.getvalue())
    filename = f"tasks_{start_date}_to_{end_date}.csv"
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    return response


# ─── Overdue Tasks API ────────────────────────────────────────

@app.route('/api/overdue')
@login_required
def api_overdue():
    """Get all overdue tasks (past due_date, not done)."""
    db = get_db()
    today = date.today().isoformat()

    query = """
        SELECT t.*, u1.display_name as assigned_name, u1.avatar_color,
               u2.display_name as creator_name
        FROM tasks t
        JOIN users u1 ON t.assigned_to = u1.id
        JOIN users u2 ON t.created_by = u2.id
        WHERE t.due_date < ? AND t.status != 'done'
    """
    params = [today]

    if session.get('role') != 'admin':
        query += " AND t.assigned_to = ?"
        params.append(session['user_id'])

    query += " ORDER BY t.due_date ASC"
    tasks = db.execute(query, params).fetchall()
    return jsonify([dict(t) for t in tasks])


# ─── Init & Run ─────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    print("\n✅ 团队任务管理系统已启动")
    print("📍 访问地址: http://localhost:5001")
    print("👤 默认管理员: admin / admin123\n")
    app.run(host='0.0.0.0', port=5001, debug=True)
