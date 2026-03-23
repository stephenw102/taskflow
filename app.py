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
app.secret_key = 'taskflow-secret-key-2026'
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

        CREATE INDEX IF NOT EXISTS idx_tasks_date ON tasks(task_date);
        CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks(assigned_to);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_attachments_task ON attachments(task_id);
    """)

    # Migration: add review_status column if not exists
    try:
        db.execute("ALTER TABLE tasks ADD COLUMN review_status TEXT DEFAULT ''")
        db.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

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
    return hashlib.sha256(pw.encode()).hexdigest()


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
    if user and user['password_hash'] == hash_password(data['password']):
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
    if user['password_hash'] != hash_password(data['old_password']):
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
               (SELECT COUNT(*) FROM comments WHERE task_id = t.id) as comment_count
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

    # Only admin can assign to others
    assigned_to = data.get('assigned_to', session['user_id'])
    if session.get('role') != 'admin' and assigned_to != session['user_id']:
        return jsonify({'error': '只有管理员可以给他人分配任务'}), 403

    cursor = db.execute(
        """INSERT INTO tasks (title, description, assigned_to, created_by,
           status, priority, progress, due_date, task_date, category)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (data['title'], data.get('description', ''),
         assigned_to, session['user_id'],
         data.get('status', 'todo'), data.get('priority', 'medium'),
         data.get('progress', 0), data.get('due_date'),
         data.get('task_date', date.today().isoformat()),
         data.get('category', ''))
    )
    db.commit()
    return jsonify({'ok': True, 'task_id': cursor.lastrowid})


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

    # Per-user stats for today
    users = db.execute(
        "SELECT id, display_name, avatar_color FROM users ORDER BY id"
    ).fetchall()

    user_stats = []
    for u in users:
        tasks = db.execute(
            "SELECT status, progress FROM tasks WHERE assigned_to = ? AND task_date = ?",
            (u['id'], today)
        ).fetchall()
        total = len(tasks)
        done = sum(1 for t in tasks if t['status'] == 'done')
        in_progress = sum(1 for t in tasks if t['status'] == 'in_progress')
        avg_progress = (sum(t['progress'] for t in tasks) / total) if total > 0 else 0

        user_stats.append({
            'user_id': u['id'],
            'display_name': u['display_name'],
            'avatar_color': u['avatar_color'],
            'total': total,
            'done': done,
            'in_progress': in_progress,
            'todo': total - done - in_progress,
            'avg_progress': round(avg_progress),
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
