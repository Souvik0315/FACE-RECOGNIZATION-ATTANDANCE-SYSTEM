from flask import (
    Flask,
    flash,
    render_template,
    render_template_string,
    request,
    redirect,
    url_for,
    session,
    jsonify,
    Response,
    send_from_directory,
)

from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

from database import get_connection
from face_engine import get_face_encoding
from face_recognition_engine import recognize_face, mark_attendance

from datetime import datetime, date
import calendar
import traceback
import os
import base64

try:
    import holidays as holidays_library
except ImportError:
    holidays_library = None

import os
import base64
import cv2
import numpy as np
import csv
import io
import secrets
from dotenv import load_dotenv

load_dotenv()


# =========================================================
# FLASK APP
# =========================================================

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or "facetrack-local-dev-key-change-this-123456"
if not os.environ.get("FLASK_SECRET_KEY"):
    app.logger.warning("FLASK_SECRET_KEY is unset; sessions will reset on restart. Configure it in .env.")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=__import__("datetime").timedelta(hours=8),
)       


@app.before_request
def ensure_csrf_token():
    if not session.get("_csrf_token"):
        session["_csrf_token"] = secrets.token_urlsafe(32)



@app.before_request
def protect_form_posts():
    if request.method != "POST":
        return None

    if request.endpoint in {"api_recognize", "employee_api_recognize"}:
        return None

    submitted = (
        request.form.get("csrf_token")
        or request.headers.get("X-CSRF-Token")
        or ""
    )

    expected = session.get("_csrf_token", "")

    if (
        not submitted
        or not expected
        or not secrets.compare_digest(submitted, expected)
    ):
        app.logger.warning(
            "CSRF validation failed: endpoint=%s, token_submitted=%s, "
            "session_token_present=%s",
            request.endpoint,
            bool(submitted),
            bool(expected),
        )
        return (
            "Security token missing or expired. "
            "Please return to the previous page, refresh, and try again.",
            400,
        )

    return None


@app.context_processor
def inject_csrf_token():
    def csrf_token():
        token = session.get("_csrf_token")

        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token

        return token

    return {"csrf_token": csrf_token}

def write_audit_log(action, entity_type=None, entity_id=None, details=None):
    connection = None
    cursor = None
    try:
        connection = get_connection()
        if not connection:
            return
        cursor = connection.cursor()
        cursor.execute(
            """INSERT INTO admin_audit_logs
               (admin_id, admin_email, action, entity_type, entity_id, details, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, NOW())""",
            (session.get("admin_id"), session.get("admin_email"), action,
             entity_type, str(entity_id) if entity_id is not None else None,
             (details or "")[:1000]),
        )
        connection.commit()
    except Exception:
        app.logger.exception("Audit logging failed")
        if connection:
            try: connection.rollback()
            except Exception: pass
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

def start_attendance_session(employee_id, confidence=None, method="Face Recognition"):
    connection = get_connection()
    if connection is None:
        return False, "Database connection failed."
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id FROM employee_attendance_sessions WHERE employee_id=%s AND check_out_at IS NULL LIMIT 1",
            (employee_id,))
        if cursor.fetchone():
            return False, "An attendance session is already open."
        cursor.execute(
            """INSERT INTO employee_attendance_sessions
               (employee_id, work_date, check_in_at, check_out_at, confidence, method)
               VALUES (%s, CURDATE(), NOW(), NULL, %s, %s)""",
            (employee_id, confidence, method))
        connection.commit()
        return True, "Check-in recorded."
    except Exception:
        connection.rollback()
        app.logger.exception("Could not start attendance session")
        return False, "Could not start attendance session. Check database migration."
    finally:
        cursor.close()
        connection.close()

def end_attendance_session(employee_id):
    connection = get_connection()
    if connection is None:
        return False, "Database connection failed."
    cursor = connection.cursor()
    try:
        cursor.execute(
            """UPDATE employee_attendance_sessions SET check_out_at=NOW()
               WHERE employee_id=%s AND check_out_at IS NULL
               ORDER BY check_in_at DESC LIMIT 1""", (employee_id,))
        changed = cursor.rowcount
        connection.commit()
        return (True, "Check-out recorded.") if changed else (False, "No open session was found.")
    except Exception:
        connection.rollback()
        app.logger.exception("Could not end attendance session")
        return False, "Could not check out. Check database migration."
    finally:
        cursor.close()
        connection.close()


# =========================================================
# FOLDERS
# =========================================================

UPLOAD_FOLDER = "faces"
REGISTRATION_FACE_FOLDER = "employee_faces"
ID_DOCUMENT_FOLDER = "employee_documents"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(REGISTRATION_FACE_FOLDER, exist_ok=True)
os.makedirs(ID_DOCUMENT_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


# =========================================================
# ALLOWED FILES
# =========================================================

ALLOWED_EXTENSIONS = {
    "jpg",
    "jpeg",
    "png"
}


def allowed_file(filename):
    return (
        filename
        and "." in filename
        and filename.rsplit(".", 1)[1].lower()
        in ALLOWED_EXTENSIONS
    )


# =========================================================
# AUTH HELPERS
# =========================================================

def admin_required():
    return "admin_id" in session


def employee_required():
    return "employee_id" in session


WEEKDAY_NAMES = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}


def get_calendar_holidays(year, month):
    """India/West Bengal public holidays plus organization-defined holidays."""
    result = {}
    if holidays_library is not None:
        try:
            public_days = holidays_library.country_holidays("IN", subdiv="WB", years=[year])
            for holiday_date, holiday_name in public_days.items():
                if holiday_date.month == month:
                    result[holiday_date.isoformat()] = str(holiday_name)
        except Exception:
            app.logger.exception("Automatic public-holiday lookup failed")
    connection = get_connection()
    if connection is not None:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """SELECT holiday_date, holiday_name FROM organization_holidays
                   WHERE holiday_date >= %s AND holiday_date <= %s""",
                (date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]))
            )
            for row in cursor.fetchall():
                holiday_date = row["holiday_date"]
                key = holiday_date.isoformat() if hasattr(holiday_date, "isoformat") else str(holiday_date)[:10]
                result[key] = row["holiday_name"]
        except Exception:
            app.logger.warning("Custom holiday table unavailable; run the holiday migration.")
        finally:
            cursor.close()
            connection.close()
    return result


def get_holiday_reason(employee_id, check_date):
    holiday_map = get_calendar_holidays(check_date.year, check_date.month)
    if check_date.isoformat() in holiday_map:
        return holiday_map[check_date.isoformat()]
    connection = get_connection()
    if connection is None:
        return None
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT weekly_off_days FROM employees WHERE employee_id=%s LIMIT 1", (employee_id,))
        row = cursor.fetchone() or {}
        raw = row.get("weekly_off_days") or "Sun"
        weekly_offs = {part.strip()[:3].title() for part in str(raw).replace(";", ",").split(",") if part.strip()}
        if check_date.strftime("%a") in weekly_offs:
            return "Weekly Holiday"
        return None
    except Exception:
        app.logger.exception("Could not check employee holiday schedule")
        return None
    finally:
        cursor.close()
        connection.close()


# =========================================================
# HOME / PORTAL
# =========================================================

@app.route("/")
def login():

    if "admin_id" in session:
        return redirect(url_for("dashboard"))

    if "employee_id" in session:
        return redirect(url_for("employee_dashboard"))

    return render_template("portal.html")


# =========================================================
# ADMIN LOGIN PAGE
# =========================================================

@app.route("/admin/login", methods=["GET"])
def admin_login():

    if "admin_id" in session:
        return redirect(url_for("dashboard"))

    return render_template("admin_login.html")


# =========================================================
# ADMIN LOGIN PROCESS
# =========================================================

@app.route("/login", methods=["POST"])
def login_process():

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()

    connection = get_connection()

    if connection is None:
        return "Database connection failed.", 500

    cursor = connection.cursor(dictionary=True)

    try:

        cursor.execute(
            """
            SELECT
                id,
                email,
                password,
                name
            FROM admins
            WHERE email = %s
            LIMIT 1
            """,
            (email,)
        )

        admin = cursor.fetchone()

        if admin is None:

            return render_template(
                "admin_login.html",
                error="Invalid email or password"
            )

        stored_password = str(admin.get("password") or "")
        try:
            password_correct = check_password_hash(stored_password, password)
        except (ValueError, TypeError):
            password_correct = False

        # Upgrade legacy plaintext passwords after a successful login.
        if not password_correct and secrets.compare_digest(stored_password, password):
            password_correct = True
            try:
                cursor.execute("UPDATE admins SET password = %s WHERE id = %s",
                               (generate_password_hash(password), admin["id"]))
                connection.commit()
            except Exception:
                connection.rollback()
                app.logger.exception("Could not upgrade legacy admin password hash")

        if not password_correct:
            return render_template("admin_login.html", error="Invalid email or password")

        session.clear()

        session["admin_id"] = admin["id"]
        session["admin_email"] = admin["email"]
        session["admin_name"] = admin["name"]

        return redirect(
            url_for("dashboard")
        )

    except Exception as error:

        return f"Login error: {error}", 500

    finally:

        cursor.close()
        connection.close()


# =========================================================
# EMPLOYEE LOGIN PAGE
# =========================================================

@app.route("/employee/login", methods=["GET"])
def employee_login():

    if "employee_id" in session:
        return redirect(
            url_for("employee_dashboard")
        )

    return render_template(
        "employee_login.html"
    )


# =========================================================
# EMPLOYEE LOGIN PROCESS
# =========================================================
#
# EMPLOYEE LOGIN NOW USES:
# Employee ID + Password
#
# NOT email.
# =========================================================

@app.route("/employee/login", methods=["POST"])
def employee_login_process():

    employee_id = request.form.get(
        "employee_id",
        ""
    ).strip()

    password = request.form.get(
        "password",
        ""
    ).strip()

    if not employee_id or not password:

        return render_template(
            "employee_login.html",
            error="Please enter Employee ID and Password."
        )

    connection = get_connection()

    if connection is None:
        return "Database connection failed.", 500

    cursor = connection.cursor(dictionary=True)

    try:

        cursor.execute(
            """
            SELECT
                id,
                employee_id,
                name,
                email,
                department,
                designation,
                phone,
                password_hash,
                face_encoding
            FROM employees
            WHERE employee_id = %s
              AND registration_status = 'Approved'
              AND is_active = 1
            LIMIT 1
            """,
            (employee_id,)
        )

        employee = cursor.fetchone()

        if employee is None:

            return render_template(
                "employee_login.html",
                error="Invalid Employee ID or Password."
            )

        password_hash = employee.get("password_hash")

        if not password_hash:

            return render_template(
                "employee_login.html",
                error=(
                    "Your account does not have a password yet. "
                    "Please contact the administrator."
                )
            )

        try:
            password_correct = check_password_hash(
                password_hash,
                password
            )
        except Exception:
            password_correct = False

        if not password_correct:

            return render_template(
                "employee_login.html",
                error="Invalid Employee ID or Password."
            )

        # Employee must have an approved face.
        if not employee.get("face_encoding"):

            return render_template(
                "employee_login.html",
                error=(
                    "Your registration is approved, "
                    "but your face is not registered yet."
                )
            )

        session.clear()

        session["employee_id"] = employee["employee_id"]
        session["employee_name"] = employee["name"]
        session["employee_email"] = employee["email"]

        return redirect(
            url_for("employee_dashboard")
        )

    except Exception as error:

        return f"Employee login error: {error}", 500

    finally:

        cursor.close()
        connection.close()


# =========================================================
# ADMIN LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# =========================================================
# EMPLOYEE LOGOUT
# =========================================================

@app.route("/employee/logout")
def employee_logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# =========================================================
# ADMIN DASHBOARD
# =========================================================



@app.route("/dashboard")
def dashboard():
    if not admin_required():
        return redirect(url_for("admin_login"))

    connection = get_connection()
    if connection is None:
        return "Database connection failed. Check database.py and your .env settings.", 500

    cursor = connection.cursor(dictionary=True)

    try:
        # Detect whether the optional upgrade column exists. This keeps the
        # dashboard usable before/after the safe migration is applied.
        cursor.execute("""
            SELECT COUNT(*) AS found
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'employees'
              AND COLUMN_NAME = 'is_active'
        """)
        has_is_active = bool(cursor.fetchone()["found"])
        active_filter = " AND is_active = 1" if has_is_active else ""

        cursor.execute(f"""
            SELECT COUNT(*) AS total
            FROM employees
            WHERE registration_status = 'Approved'{active_filter}
        """)
        total_employees = int(cursor.fetchone()["total"] or 0)

        cursor.execute("""
            SELECT COUNT(DISTINCT employee_id) AS total
            FROM employee_attendance
            WHERE attendance_date = CURDATE() AND status = 'Present'
        """)
        today_attendance = int(cursor.fetchone()["total"] or 0)

        cursor.execute(f"""
            SELECT COUNT(*) AS total
            FROM employees
            WHERE registration_status = 'Approved'
              AND face_encoding IS NOT NULL
              AND LENGTH(TRIM(face_encoding)) > 0{active_filter}
        """)
        registered_faces = int(cursor.fetchone()["total"] or 0)

        today_attendance_percentage = round(
            today_attendance / total_employees * 100, 1
        ) if total_employees else 0
        absent_today = max(total_employees - today_attendance, 0)

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM employee_registration_requests
            WHERE status = 'Pending'
        """)
        pending_requests = int(cursor.fetchone()["total"] or 0)

        cursor.execute("""
            SELECT attendance_date, COUNT(DISTINCT employee_id) AS total_present
            FROM employee_attendance
            WHERE attendance_date >= DATE_SUB(CURDATE(), INTERVAL 6 DAY)
              AND attendance_date <= CURDATE()
              AND status = 'Present'
            GROUP BY attendance_date
            ORDER BY attendance_date
        """)
        weekly_rows = cursor.fetchall()
        weekly_by_date = {row["attendance_date"]: int(row["total_present"] or 0)
                          for row in weekly_rows}

        weekly_attendance = []
        for days_ago in range(6, -1, -1):
            cursor.execute("SELECT DATE_SUB(CURDATE(), INTERVAL %s DAY) AS day", (days_ago,))
            day = cursor.fetchone()["day"]
            weekly_attendance.append({
                "date": day,
                "label": day.strftime("%a"),
                "short_date": day.strftime("%b %d"),
                "total_present": weekly_by_date.get(day, 0),
                "absent_estimate": max(total_employees - weekly_by_date.get(day, 0), 0),
            })

        cursor.execute("""
            SELECT a.id, a.employee_id, e.name, e.department, e.designation,
                   a.attendance_date, a.attendance_time, a.status, a.confidence, a.method
            FROM employee_attendance a
            JOIN employees e ON a.employee_id = e.employee_id
            ORDER BY a.attendance_date DESC, a.attendance_time DESC
            LIMIT 10
        """)
        recent_attendance = cursor.fetchall()

        return render_template(
            "dashboard.html",
            total_employees=total_employees,
            today_attendance=today_attendance,
            registered_faces=registered_faces,
            today_attendance_percentage=today_attendance_percentage,
            absent_today=absent_today,
            pending_requests=pending_requests,
            weekly_attendance=weekly_attendance,
            recent_attendance=recent_attendance,
        )
    except Exception:
        app.logger.exception("Admin dashboard failed")
        return "Dashboard failed. Check the VS Code terminal for the exact database error.", 500
    finally:
        cursor.close()
        connection.close()



# =========================================================
# ADMIN LEAVE MANAGEMENT + EMPLOYEE LEAVE REQUESTS
# =========================================================

LEAVE_TYPES = ("Casual Leave", "Sick Leave", "Earned Leave", "Unpaid Leave")


@app.route("/admin/leave-management", methods=["GET", "POST"])
def admin_leave_management():
    """Admin-only leave entry and approval/rejection page."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    connection = get_connection()
    if connection is None:
        return "Database connection failed.", 500
    cursor = connection.cursor(dictionary=True)
    try:
        if request.method == "POST":
            action = (request.form.get("action") or "").strip()
            admin_identity = str(session.get("admin_id", ""))
            if action == "add":
                employee_id = (request.form.get("employee_id") or "").strip()
                leave_type = (request.form.get("leave_type") or "").strip()
                reason = (request.form.get("reason") or "").strip()
                try:
                    start_date = date.fromisoformat(request.form.get("start_date", ""))
                    end_date = date.fromisoformat(request.form.get("end_date", ""))
                except (TypeError, ValueError):
                    flash("Enter valid leave dates.", "danger")
                    return redirect(url_for("admin_leave_management"))
                if not employee_id or leave_type not in LEAVE_TYPES or end_date < start_date:
                    flash("Check employee code, leave type and date range.", "danger")
                    return redirect(url_for("admin_leave_management"))
                cursor.execute("SELECT employee_id, joining_date FROM employees WHERE employee_id=%s LIMIT 1", (employee_id,))
                employee = cursor.fetchone()
                if not employee:
                    flash("Employee code was not found.", "danger")
                    return redirect(url_for("admin_leave_management"))
                joining_date = employee.get("joining_date")
                if isinstance(joining_date, str):
                    try:
                        joining_date = date.fromisoformat(joining_date[:10])
                    except ValueError:
                        joining_date = None
                if joining_date and start_date < joining_date:
                    flash("Leave cannot start before the employee's joining date.", "danger")
                    return redirect(url_for("admin_leave_management"))
                cursor.execute("""SELECT id FROM employee_leaves
                    WHERE employee_id=%s AND status IN ('Pending','Approved')
                    AND start_date <= %s AND end_date >= %s LIMIT 1""", (employee_id, end_date, start_date))
                if cursor.fetchone():
                    flash("This employee already has overlapping pending or approved leave.", "danger")
                    return redirect(url_for("admin_leave_management"))
                cursor.execute("""INSERT INTO employee_leaves
                    (employee_id, leave_type, start_date, end_date, reason, status, approved_by)
                    VALUES (%s, %s, %s, %s, %s, 'Approved', %s)""",
                    (employee_id, leave_type, start_date, end_date, reason, admin_identity))
                leave_id = cursor.lastrowid
                connection.commit()
                write_audit_log("leave_created_approved", "employee_leave", leave_id,
                                f"Admin entered approved leave for employee {employee_id}")
                flash("Leave added and approved. The employee calendar will reflect it automatically.", "success")
                return redirect(url_for("admin_leave_management"))

            if action in ("approve", "reject", "cancel"):
                leave_id = request.form.get("leave_id", type=int)
                if not leave_id:
                    flash("Invalid leave record.", "danger")
                    return redirect(url_for("admin_leave_management"))
                if action == "approve":
                    cursor.execute("""SELECT id, employee_id, start_date, end_date FROM employee_leaves
                        WHERE id=%s AND status='Pending' FOR UPDATE""", (leave_id,))
                    pending = cursor.fetchone()
                    if not pending:
                        flash("Pending leave request not found.", "warning")
                        return redirect(url_for("admin_leave_management"))
                    cursor.execute("""SELECT id FROM employee_leaves WHERE employee_id=%s
                        AND id<>%s AND status='Approved' AND start_date <= %s AND end_date >= %s LIMIT 1""",
                        (pending["employee_id"], leave_id, pending["end_date"], pending["start_date"]))
                    if cursor.fetchone():
                        flash("Cannot approve: the employee has overlapping approved leave.", "danger")
                        return redirect(url_for("admin_leave_management"))
                    new_status = "Approved"
                elif action == "reject":
                    new_status = "Rejected"
                else:
                    new_status = "Cancelled"
                if action in ("approve", "reject"):
                    cursor.execute("UPDATE employee_leaves SET status=%s, approved_by=%s WHERE id=%s AND status='Pending'",
                                   (new_status, admin_identity, leave_id))
                else:
                    cursor.execute("UPDATE employee_leaves SET status='Cancelled', approved_by=%s WHERE id=%s AND status='Approved'",
                                   (admin_identity, leave_id))
                changed = cursor.rowcount
                connection.commit()
                if changed:
                    write_audit_log(f"leave_{new_status.lower()}", "employee_leave", leave_id)
                    flash(f"Leave {new_status.lower()} successfully.", "success")
                else:
                    flash("The leave record was not in a status that can be changed.", "warning")
                return redirect(url_for("admin_leave_management"))
            flash("Unknown action.", "danger")
            return redirect(url_for("admin_leave_management"))

        cursor.execute("""SELECT l.id, l.employee_id, e.name AS employee_name, l.leave_type,
            l.start_date, l.end_date, l.reason, l.status, l.approved_by, l.created_at
            FROM employee_leaves l LEFT JOIN employees e ON e.employee_id=l.employee_id
            ORDER BY l.created_at DESC, l.id DESC""")
        leave_records = cursor.fetchall()
        return render_template(
                "admin_leave_management.html",
                leave_records=leave_records,
                leave_types=LEAVE_TYPES
        )
    except Exception:
        connection.rollback()
        app.logger.exception("Admin leave management failed")
        return "Unable to process leave management. Check the VS Code terminal for details.", 500
    finally:
        cursor.close()
        connection.close()


@app.route("/employee/leave-requests", methods=["GET", "POST"])
def employee_leave_requests():
    """Employees can submit requests; only admins can approve or reject them."""
    if not employee_required():
        return redirect(url_for("employee_login"))
    employee_id = str(session.get("employee_id", ""))
    connection = get_connection()
    if connection is None:
        return "Database connection failed.", 500
    cursor = connection.cursor(dictionary=True)
    try:
        if request.method == "POST":
            leave_type = (request.form.get("leave_type") or "").strip()
            reason = (request.form.get("reason") or "").strip()
            try:
                start_date = date.fromisoformat(request.form.get("start_date", ""))
                end_date = date.fromisoformat(request.form.get("end_date", ""))
            except (TypeError, ValueError):
                flash("Enter valid leave dates.", "danger")
                return redirect(url_for("employee_leave_requests"))
            if leave_type not in LEAVE_TYPES or end_date < start_date:
                flash("Check the leave type and date range.", "danger")
                return redirect(url_for("employee_leave_requests"))
            cursor.execute("SELECT joining_date FROM employees WHERE employee_id=%s LIMIT 1", (employee_id,))
            employee = cursor.fetchone()
            if not employee:
                session.clear()
                return redirect(url_for("employee_login"))
            joining_date = employee.get("joining_date")
            if isinstance(joining_date, str):
                try:
                    joining_date = date.fromisoformat(joining_date[:10])
                except ValueError:
                    joining_date = None
            if joining_date and start_date < joining_date:
                flash("Leave cannot start before your joining date.", "danger")
                return redirect(url_for("employee_leave_requests"))
            cursor.execute("""SELECT id FROM employee_leaves WHERE employee_id=%s
                AND status IN ('Pending','Approved') AND start_date <= %s AND end_date >= %s LIMIT 1""",
                (employee_id, end_date, start_date))
            if cursor.fetchone():
                flash("You already have overlapping pending or approved leave.", "danger")
                return redirect(url_for("employee_leave_requests"))
            cursor.execute("""INSERT INTO employee_leaves
                (employee_id, leave_type, start_date, end_date, reason, status)
                VALUES (%s,%s,%s,%s,%s,'Pending')""", (employee_id, leave_type, start_date, end_date, reason))
            connection.commit()
            flash("Leave request submitted. It will appear on the calendar only after admin approval.", "success")
            return redirect(url_for("employee_leave_requests"))
        cursor.execute("""SELECT id, leave_type, start_date, end_date, reason, status, created_at
            FROM employee_leaves WHERE employee_id=%s ORDER BY created_at DESC, id DESC""", (employee_id,))
        records = cursor.fetchall()
        
        return render_template(
            "employee_leave_requests.html",
            leave_requests=records,
            leave_types=LEAVE_TYPES
        )

        
    except Exception:
        connection.rollback()
        app.logger.exception("Employee leave request failed")
        return "Unable to process leave request. Check the application log.", 500
    finally:
        cursor.close()
        connection.close()


# =========================================================
# EMPLOYEE DASHBOARD
# =========================================================

@app.route("/employee/dashboard")
def employee_dashboard():

    if not employee_required():
        return redirect(url_for("employee_login"))

    employee_id = session.get("employee_id")

    connection = get_connection()

    if connection is None:
        return "Database connection failed.", 500

    cursor = connection.cursor(dictionary=True)

    try:
        # Employee information
        cursor.execute(
            """
            SELECT
                id,
                employee_id,
                name,
                email,
                phone,
                department,
                designation,
                photo_path
            FROM employees
            WHERE employee_id = %s
            LIMIT 1
            """,
            (employee_id,)
        )

        employee = cursor.fetchone()

        if employee is None:
            session.clear()
            return redirect(url_for("employee_login"))

        # Total present
        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM employee_attendance
            WHERE employee_id = %s
              AND status = 'Present'
            """,
            (employee_id,)
        )

        total_present = cursor.fetchone()["total"]

        # Present this month
        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM employee_attendance
            WHERE employee_id = %s
              AND status = 'Present'
              AND YEAR(attendance_date) = YEAR(CURDATE())
              AND MONTH(attendance_date) = MONTH(CURDATE())
            """,
            (employee_id,)
        )

        this_month_present = cursor.fetchone()["total"]
                # Monthly attendance for the last 6 months
        cursor.execute(
            """
            SELECT
                DATE_FORMAT(attendance_date, '%Y-%m') AS month_key,
                COUNT(*) AS total
            FROM employee_attendance
            WHERE employee_id = %s
              AND status = 'Present'
              AND attendance_date >= DATE_SUB(
                    DATE_FORMAT(CURDATE(), '%Y-%m-01'),
                    INTERVAL 5 MONTH
              )
              AND attendance_date < DATE_ADD(
                    DATE_FORMAT(CURDATE(), '%Y-%m-01'),
                    INTERVAL 1 MONTH
              )
            GROUP BY DATE_FORMAT(attendance_date, '%Y-%m')
            ORDER BY month_key
            """,
            (employee_id,)
        )

        monthly_attendance = cursor.fetchall()

        # Last attendance
        cursor.execute(
            """
            SELECT
                attendance_date,
                attendance_time,
                status,
                confidence,
                method
            FROM employee_attendance
            WHERE employee_id = %s
            ORDER BY attendance_date DESC,
                     attendance_time DESC
            LIMIT 1
            """,
            (employee_id,)
        )

        last_attendance = cursor.fetchone()

        # Today's attendance
        cursor.execute(
            """
            SELECT
                attendance_date,
                attendance_time,
                status,
                confidence,
                method
            FROM employee_attendance
            WHERE employee_id = %s
              AND attendance_date = CURDATE()
            ORDER BY attendance_time DESC
            LIMIT 1
            """,
            (employee_id,)
        )

        today_attendance = cursor.fetchone()

        # Recent attendance
        cursor.execute(
            """
            SELECT
                attendance_date,
                attendance_time,
                status,
                confidence,
                method
            FROM employee_attendance
            WHERE employee_id = %s
            ORDER BY attendance_date DESC,
                     attendance_time DESC
            LIMIT 10
            """,
            (employee_id,)
        )

        recent_attendance = cursor.fetchall()

        # Face registration status
        cursor.execute(
            """
            SELECT
                CASE
                    WHEN face_encoding IS NOT NULL
                     AND LENGTH(TRIM(face_encoding)) > 0
                    THEN 1
                    ELSE 0
                END AS face_registered
            FROM employees
            WHERE employee_id = %s
            LIMIT 1
            """,
            (employee_id,)
        )

        face_status = cursor.fetchone()

        face_registered = (
            face_status is not None
            and face_status["face_registered"] == 1
        )

        return render_template(
            "employee_dashboard.html",
            employee=employee,
            total_present=total_present,
            this_month_present=this_month_present,
            last_attendance=last_attendance,
            today_attendance=today_attendance,
            recent_attendance=recent_attendance,
            face_registered=face_registered,
            monthly_attendance=monthly_attendance
        )

    except Exception as error:
        import traceback
        print("EMPLOYEE DASHBOARD ERROR:")
        traceback.print_exc()
        return "Unable to load employee dashboard.", 500

    finally:
        cursor.close()
        connection.close()



# =========================================================
# EMPLOYEE PROFILE
# =========================================================

@app.route("/employee/profile")
def employee_profile():

    if not employee_required():
        return redirect(url_for("employee_login"))

    employee_id = session.get("employee_id")
    connection = get_connection()

    if connection is None:
        return "Database connection failed.", 500

    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                employee_id,
                name,
                email,
                phone,
                department,
                designation,
                photo_path
            FROM employees
            WHERE employee_id = %s
            LIMIT 1
            """,
            (employee_id,)
        )

        employee = cursor.fetchone()

        if employee is None:
            session.clear()
            return redirect(url_for("employee_login"))

        return render_template(
            "employee_profile.html",
            employee=employee
        )

    except Exception:
        import traceback
        traceback.print_exc()
        return "Unable to load employee profile.", 500

    finally:
        cursor.close()
        connection.close()




# =========================================================
# EMPLOYEE CHANGE PASSWORD
# =========================================================
@app.route("/employee/change-password", methods=["GET", "POST"])
def employee_change_password():

    if not employee_required():
        return redirect(url_for("employee_login"))

    employee_id = session.get("employee_id")

    if request.method == "POST":

        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not current_password or not new_password or not confirm_password:
            return render_template(
                "employee_change_password.html",
                error="Please fill in all three password fields."
            )

        if len(new_password) < 6:
            return render_template(
                "employee_change_password.html",
                error="Your new password must contain at least 6 characters."
            )

        if new_password != confirm_password:
            return render_template(
                "employee_change_password.html",
                error="The new passwords do not match."
            )

        if current_password == new_password:
            return render_template(
                "employee_change_password.html",
                error="Your new password must be different from your current password."
            )

        connection = get_connection()

        if connection is None:
            return "Database connection failed.", 500

        cursor = connection.cursor(dictionary=True)

        try:
            cursor.execute(
                """
                SELECT password_hash
                FROM employees
                WHERE employee_id = %s
                LIMIT 1
                """,
                (employee_id,)
            )

            employee = cursor.fetchone()

            if employee is None:
                session.clear()
                return redirect(url_for("employee_login"))

            password_hash = employee.get("password_hash")

            if not password_hash or not check_password_hash(
                password_hash, current_password
            ):
                return render_template(
                    "employee_change_password.html",
                    error="Your current password is incorrect."
                )

            new_password_hash = generate_password_hash(new_password)

            cursor.execute(
                """
                UPDATE employees
                SET password_hash = %s
                WHERE employee_id = %s
                """,
                (new_password_hash, employee_id)
            )

            connection.commit()

            return render_template(
                "employee_change_password.html",
                success="Password changed successfully!"
            )

        except Exception:
            connection.rollback()
            traceback.print_exc()

            return render_template(
                "employee_change_password.html",
                error="Unable to change your password. Please try again."
            ), 500

        finally:
            cursor.close()
            connection.close()

    return render_template("employee_change_password.html")


# =========================================================
# EMPLOYEE ATTENDANCE CALENDAR
# =========================================================

@app.route("/employee/attendance-calendar")
def employee_attendance_calendar():

    if not employee_required():
        return redirect(url_for("employee_login"))

    import calendar
    import traceback
    from datetime import date

    employee_id = session.get("employee_id")
    today = date.today()

    # Validate the requested month and year
    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))

        if not 1 <= month <= 12 or not 2000 <= year <= 2100:
            raise ValueError

    except (ValueError, TypeError):
        year = today.year
        month = today.month

    first_day = date(year, month, 1)

    last_day = date(
        year,
        month,
        calendar.monthrange(year, month)[1]
    )

    # Previous and next month
    previous_month = 12 if month == 1 else month - 1
    previous_year = year - 1 if month == 1 else year

    next_month = 1 if month == 12 else month + 1
    next_year = year + 1 if month == 12 else year

    connection = get_connection()

    if connection is None:
        return "Database connection failed.", 500

    cursor = connection.cursor(dictionary=True)

    try:
        # Load this employee's weekly-off schedule and the month's holidays.
        cursor.execute("SELECT weekly_off_days FROM employees WHERE employee_id=%s LIMIT 1", (employee_id,))
        employee_schedule = cursor.fetchone() or {}
        weekly_off_days = {
            part.strip()[:3].title()
            for part in str(employee_schedule.get("weekly_off_days") or "Sun").replace(";", ",").split(",")
            if part.strip() and part.strip()[:3].title() in WEEKDAY_NAMES
        }
        holiday_by_date = get_calendar_holidays(year, month)
        weekly_off_dates = {
            date(year, month, day_number).isoformat()
            for day_number in range(1, last_day.day + 1)
            if date(year, month, day_number).strftime("%a") in weekly_off_days
        }

        # Fetch only the logged-in employee's attendance
        cursor.execute(
            """
            SELECT
                attendance_date,
                attendance_time,
                status,
                confidence,
                method
            FROM employee_attendance
            WHERE employee_id = %s
              AND attendance_date >= %s
              AND attendance_date <= %s
            ORDER BY attendance_date DESC,
                     attendance_time DESC
            """,
            (employee_id, first_day, last_day)
        )

        records = cursor.fetchall()
        attendance_by_date = {}

        # Store one actual attendance record per date for the calendar display.
        for record in records:
            record_date = record["attendance_date"]
            date_key = record_date.strftime("%Y-%m-%d") if hasattr(record_date, "strftime") else str(record_date)[:10]
            if date_key not in attendance_by_date:
                attendance_by_date[date_key] = record

        # Approved leave appears on the calendar but is not inserted as attendance.
        cursor.execute(
            """SELECT start_date, end_date, leave_type FROM employee_leaves
               WHERE employee_id=%s AND status='Approved'
                 AND start_date <= %s AND end_date >= %s""",
            (employee_id, last_day, first_day),
        )
        approved_leave_rows = cursor.fetchall()
        approved_leave_by_date = {}
        from datetime import timedelta
        for leave_row in approved_leave_rows:
            leave_start = leave_row["start_date"]
            leave_end = leave_row["end_date"]
            if isinstance(leave_start, str):
                leave_start = date.fromisoformat(leave_start[:10])
            if isinstance(leave_end, str):
                leave_end = date.fromisoformat(leave_end[:10])
            current_leave_date = max(leave_start, first_day)
            final_leave_date = min(leave_end, last_day)
            while current_leave_date <= final_leave_date:
                approved_leave_by_date[current_leave_date.isoformat()] = leave_row["leave_type"]
                current_leave_date += timedelta(days=1)

        # Actual attendance takes priority; otherwise show a calendar-only leave status.
        for leave_date_key, leave_type_value in approved_leave_by_date.items():
            if leave_date_key not in attendance_by_date:
                attendance_by_date[leave_date_key] = {
                    "attendance_date": date.fromisoformat(leave_date_key),
                    "attendance_time": None,
                    "status": "On Leave",
                    "confidence": None,
                    "method": leave_type_value,
                }

        total_present = sum(
            1 for record in records
            if record["status"] == "Present"
        )
        
       
        # ==========================================
        # MONTHLY ATTENDANCE SUMMARY
        # ==========================================

        total_present = sum(
            1 for record in attendance_by_date.values()
            if record["status"] == "Present"
        )

        total_absent = 0
        total_weekly_holidays = 0
        total_public_holidays = 0
        total_working_days = 0

        attendance_percentage = 0

        # Get the employee's actual joining date
        cursor.execute(
            """
            SELECT joining_date
            FROM employees
            WHERE employee_id = %s
            """,
            (employee_id,)
        )
        employee_info = cursor.fetchone() or {}
        joining_date = employee_info.get("joining_date")
        if isinstance(joining_date, str):
            try:
                joining_date = date.fromisoformat(joining_date[:10])
            except ValueError:
                joining_date = None

        elif joining_date is not None and hasattr(joining_date, "date"):
            joining_date = joining_date.date()

        for day_number in range(1, last_day.day + 1):
            current_date = date(year, month, day_number)
            date_key = current_date.isoformat()

            # Do not count dates before the employee joined
            if joining_date and current_date < joining_date:
                continue

            # Ignore future dates first
            if current_date > today:
                continue

            # Count weekly holidays separately
            if date_key in weekly_off_dates:
                total_weekly_holidays += 1
                continue

            # Count public or organization holidays separately
            if holiday_by_date.get(date_key):
                total_public_holidays += 1
                continue

            record = attendance_by_date.get(date_key)

            if record and record["status"] == "Present":
                total_working_days += 1

            elif record is None and current_date < today:
                total_absent += 1
                total_working_days += 1

        attendance_percentage = (
            round(total_present / total_working_days * 100, 1)
            if total_working_days > 0
            else 0
        )
# Build and display the attendance calendar                
        # Get the selected date, if provided
        selected_date = request.args.get("date", "")
        selected_record = None

        if selected_date:
            try:
                parsed_date = date.fromisoformat(selected_date)

                if first_day <= parsed_date <= last_day:
                    selected_record = attendance_by_date.get(
                        parsed_date.isoformat()
                    )
                else:
                    selected_date = ""

            except ValueError:
                selected_date = ""

        # Build the attendance calendar
        month_weeks = calendar.Calendar(
            firstweekday=0
        ).monthdayscalendar(year, month)

        return render_template(
            "employee_attendance_calendar.html",
            employee_name=session.get("employee_name", "Employee"),
            employee_id=employee_id,
            joining_date=joining_date,
            year=year,
            month=month,
            month_name=calendar.month_name[month],
            month_weeks=month_weeks,
            today_date=today.strftime("%Y-%m-%d"),
            attendance_by_date=attendance_by_date,
            total_present=total_present,
            total_absent=total_absent,
            total_weekly_holidays=total_weekly_holidays,
            total_public_holidays=total_public_holidays,
            total_working_days=total_working_days,
            attendance_percentage=attendance_percentage,
            previous_month=previous_month,
            previous_year=previous_year,
            next_month=next_month,
            next_year=next_year,
            selected_date=selected_date,
            selected_record=selected_record,
            weekly_off_days=weekly_off_days,
            weekly_off_dates=weekly_off_dates,
            holiday_by_date=holiday_by_date,
            approved_leave_by_date=approved_leave_by_date
        )
    except Exception:
        print("EMPLOYEE CALENDAR ERROR:")
        traceback.print_exc()
        return "Unable to load attendance calendar.", 500

    finally:
        cursor.close()
        connection.close()
        
# =========================================================
# EMPLOYEE FACE RECOGNITION PAGE
# =========================================================

@app.route("/employee/recognition")
def employee_recognition():

    if not employee_required():

        return redirect(
            url_for("employee_login")
        )

    employee_name = session.get(
        "employee_name",
        "Employee"
    )

    employee_id = session.get(
        "employee_id",
        ""
    )

    return render_template(
    "employee_recognition.html",
    employee_name=employee_name,
    employee_id=employee_id
)

# =========================================================
# EMPLOYEE FACE RECOGNITION API
# =========================================================

@app.route(
    "/employee/api/recognize",
    methods=["POST"]
)
def employee_api_recognize():

    if not employee_required():

        return jsonify({
            "success": False,
            "message": "Unauthorized"
        }), 401

    data = request.get_json()

    if not data or "image" not in data:

        return jsonify({
            "success": False,
            "message": "No image received."
        }), 400

    try:

        image_data = data["image"]

        if "," in image_data:

            image_data = image_data.split(
                ",",
                1
            )[1]

        image_bytes = base64.b64decode(
            image_data
        )

        np_array = np.frombuffer(
            image_bytes,
            dtype=np.uint8
        )

        frame = cv2.imdecode(
            np_array,
            cv2.IMREAD_COLOR
        )

        if frame is None:

            return jsonify({
                "success": False,
                "message": "Invalid image."
            }), 400

        results = recognize_face(frame)

        logged_employee_id = session["employee_id"]

        # No face
        if not results:

            return jsonify({
                "success": False,
                "message": "No face detected."
            })

        # Check whether logged-in employee's face
        # is the recognized face.
        employee_match = None

        for result in results:

            if result.get("employee_id") == logged_employee_id:

                employee_match = result
                break

        if employee_match is None:

            return jsonify({
                "success": False,
                "message": (
                    "Face does not match your registered face."
                )
            })

        confidence = employee_match.get(
            "confidence",
            0
        )

        holiday_reason = get_holiday_reason(logged_employee_id, date.today())
        if holiday_reason:
            return jsonify({
                "success": False,
                "recognized": True,
                "attendance_marked": False,
                "holiday": True,
                "message": f"Today is your {holiday_reason}. Attendance has not been recorded."
            })

        marked = mark_attendance(logged_employee_id, confidence)
        session_started, session_message = start_attendance_session(
            logged_employee_id, confidence, "Employee Face Recognition"
        )

        return jsonify({
            "success": True,
            "recognized": True,
            "name": session.get(
                "employee_name",
                "Employee"
            ),
            "employee_id": logged_employee_id,
            "confidence": confidence,
            "attendance_marked": marked,
            "session_started": session_started,
            "session_message": session_message,
            "message": (
                "Face recognized. Daily attendance is saved."
                if marked else "Face recognized; daily attendance could not be saved."
            )
        })

    except Exception as error:

        return jsonify({
            "success": False,
            "message": str(error)
        }), 500


# =========================================================
# ADMIN EMPLOYEES PAGE
# =========================================================


@app.route("/employees")
def employees():

    if not admin_required():
        return redirect(url_for("admin_login"))

    connection = get_connection()

    if connection is None:
        return "Database connection failed.", 500

    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT
                id,
                employee_id,
                name,
                email,
                department,
                designation,
                phone,
                photo_path,
                registration_status,
                is_active,
                face_encoding,
                created_at,
                CASE
                    WHEN face_encoding IS NOT NULL
                    AND LENGTH(TRIM(face_encoding)) > 0
                    THEN 1
                    ELSE 0
                END AS face_registered
            FROM employees
            ORDER BY id DESC
        """)

        employees_list = cursor.fetchall()

        return render_template(
            "employee.html",
            employees=employees_list
        )
    except Exception:
        app.logger.exception("Employee list page failed")
        return "Employee list failed. Check the VS Code terminal.", 500

    finally:
        cursor.close()
        connection.close()



# =========================================================
# ADD EMPLOYEE FROM ADMIN
# =========================================================

@app.route(
    "/employees/add",
    methods=["POST"]
)
def add_employee():

    if not admin_required():

        return redirect(
            url_for("admin_login")
        )

    employee_id = request.form.get(
        "employee_id",
        ""
    ).strip()

    name = request.form.get(
        "name",
        ""
    ).strip()

    email = request.form.get(
        "email",
        ""
    ).strip()

    department = request.form.get(
        "department",
        ""
    ).strip()

    designation = request.form.get(
        "designation",
        ""
    ).strip()

    phone = request.form.get(
        "phone",
        ""
    ).strip()

    photo = request.files.get("photo")

    if not employee_id or not name:

        return (
            "Employee ID and Name are required.",
            400
        )

    if photo is None or not photo.filename:

        return (
            "Please capture an employee photo.",
            400
        )

    if not allowed_file(photo.filename):

        return (
            "Invalid image format. "
            "Use JPG, JPEG or PNG.",
            400
        )

    safe_employee_id = secure_filename(
        employee_id
    )

    extension = photo.filename.rsplit(
        ".",
        1
    )[1].lower()

    filename = (
        f"{safe_employee_id}_"
        f"{datetime.now().strftime('%Y%m%d%H%M%S')}"
        f".{extension}"
    )

    full_path = os.path.join(
        app.config["UPLOAD_FOLDER"],
        filename
    )

    try:

        photo.save(full_path)

        test_image = cv2.imread(
            full_path
        )

        if test_image is None:

            os.remove(full_path)

            return (
                "The captured photo could not "
                "be read.",
                400
            )

        encoding = get_face_encoding(
            full_path
        )

        if encoding is None:

            if os.path.exists(full_path):
                os.remove(full_path)

            return (
                """
                <h2>Face Registration Failed</h2>
                <p>No valid single face was found.</p>
                <p>Please try again with:</p>
                <ul>
                    <li>Good lighting</li>
                    <li>Only one person</li>
                    <li>Face facing camera</li>
                    <li>Face clearly visible</li>
                </ul>
                <a href="/employees">
                    Back to Employees
                </a>
                """,
                400
            )

        face_encoding = ",".join(
            str(value)
            for value in encoding
        )

        connection = get_connection()

        if connection is None:

            if os.path.exists(full_path):
                os.remove(full_path)

            return (
                "Database connection failed.",
                500
            )

        cursor = connection.cursor()

        try:

            cursor.execute(
                """
                SELECT id
                FROM employees
                WHERE employee_id = %s
                LIMIT 1
                """,
                (employee_id,)
            )

            if cursor.fetchone():

                connection.rollback()

                if os.path.exists(full_path):
                    os.remove(full_path)

                return (
                    "This Employee ID already exists.",
                    409
                )

            cursor.execute(
                """
                INSERT INTO employees
                (
                    employee_id,
                    name,
                    email,
                    department,
                    designation,
                    phone,
                    photo_path,
                    face_encoding,
                    registration_status,
                    approved_at
                )
                VALUES
                (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    'Approved',
                    %s
                )
                """,
                (
                    employee_id,
                    name,
                    email,
                    department,
                    designation,
                    phone,
                    full_path,
                    face_encoding,
                    datetime.now()
                )
            )

            connection.commit()
            write_audit_log("employee_created", "employee", employee_id,
                            f"Created employee {name}")

        except Exception as error:

            connection.rollback()

            if os.path.exists(full_path):
                os.remove(full_path)

            return (
                f"Error adding employee: {error}",
                500
            )

        finally:

            cursor.close()
            connection.close()

        return redirect(
            url_for("employees")
        )

    except Exception as error:

        if os.path.exists(full_path):
            os.remove(full_path)

        return (
            f"Error processing photo: {error}",
            500
        )


# =========================================================
# EDIT EMPLOYEE
# =========================================================

@app.route("/employees/edit/<int:employee_pk>", methods=["GET", "POST"])
def edit_employee(employee_pk):
    if not admin_required():
        return redirect(url_for("admin_login"))
    connection = get_connection()
    if connection is None:
        return "Database connection failed.", 500
    cursor = connection.cursor(dictionary=True)
    try:
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip()
            phone = request.form.get("phone", "").strip()
            department = request.form.get("department", "").strip()
            designation = request.form.get("designation", "").strip()
            if not name or not email:
                cursor.execute("SELECT * FROM employees WHERE id=%s", (employee_pk,))
                employee = cursor.fetchone()
                return render_template("employee_edit.html", employee=employee,
                                       error="Name and email are required."), 400
            cursor.execute(
                """UPDATE employees SET name=%s, email=%s, phone=%s,
                   department=%s, designation=%s WHERE id=%s""",
                (name, email, phone, department, designation, employee_pk))
            connection.commit()
            write_audit_log("employee_updated", "employee", employee_pk,
                            f"Updated profile for {name}")
            return redirect(url_for("employees"))
        cursor.execute("SELECT * FROM employees WHERE id=%s LIMIT 1", (employee_pk,))
        employee = cursor.fetchone()
        if not employee:
            return "Employee not found.", 404
        return render_template("employee_edit.html", employee=employee)
    except Exception:
        connection.rollback()
        app.logger.exception("Employee edit failed")
        return "Employee edit failed. Check the VS Code terminal.", 500
    finally:
        cursor.close()
        connection.close()


# =========================================================
# REACTIVATE EMPLOYEE
# =========================================================

@app.route("/employees/activate/<int:employee_pk>", methods=["POST"])
def activate_employee(employee_pk):
    if not admin_required():
        return redirect(url_for("admin_login"))
    connection = get_connection()
    if connection is None:
        return "Database connection failed.", 500
    cursor = connection.cursor()
    try:
        cursor.execute("UPDATE employees SET is_active=1 WHERE id=%s", (employee_pk,))
        if cursor.rowcount == 0:
            connection.rollback()
            return "Employee not found or already active.", 404
        connection.commit()
        write_audit_log("employee_reactivated", "employee", employee_pk, "Reactivated employee account")
    except Exception:
        connection.rollback()
        app.logger.exception("Employee reactivation failed")
        return "Employee could not be reactivated.", 500
    finally:
        cursor.close()
        connection.close()
    return redirect(url_for("employees"))


# =========================================================
# DELETE EMPLOYEE
# =========================================================

@app.route(
    "/employees/delete/<int:employee_pk>", methods=["POST"]
)
def delete_employee(employee_pk):
    """Deactivate an employee instead of deleting historical records."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    connection = get_connection()
    if connection is None:
        return "Database connection failed.", 500
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT employee_id, name FROM employees WHERE id=%s LIMIT 1", (employee_pk,))
        employee = cursor.fetchone()
        if not employee:
            return "Employee not found.", 404
        cursor.execute("UPDATE employees SET is_active=0 WHERE id=%s", (employee_pk,))
        connection.commit()
        write_audit_log("employee_deactivated", "employee", employee_pk,
                         f"Deactivated {employee['employee_id']} - {employee['name']}")
    except Exception:
        connection.rollback()
        app.logger.exception("Employee deactivation failed")
        return "Employee could not be deactivated. Check the VS Code terminal.", 500
    finally:
        cursor.close()
        connection.close()
    return redirect(url_for("employees"))


# =========================================================
# ADMIN FACE RECOGNITION PAGE
# =========================================================

@app.route("/recognition")
def recognition():

    if not admin_required():

        return redirect(
            url_for("admin_login")
        )

    return render_template(
        "recognition.html"
    )


# =========================================================
# ADMIN FACE RECOGNITION API
# =========================================================

@app.route(
    "/api/recognize",
    methods=["POST"]
)

def api_recognize():

    if not admin_required():
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        }), 401

    data = request.get_json(silent=True)

    if not data or not data.get("image"):
        return jsonify({
            "success": False,
            "message": "No image received"
        }), 400

    try:
        image_data = data["image"]

        if "," in image_data:
            image_data = image_data.split(",", 1)[1]

        image_bytes = base64.b64decode(image_data, validate=True)

        np_array = np.frombuffer(
            image_bytes,
            dtype=np.uint8
        )

        frame = cv2.imdecode(
            np_array,
            cv2.IMREAD_COLOR
        )

        if frame is None:
            return jsonify({
                "success": False,
                "message": "Invalid image"
            }), 400

        results = recognize_face(frame)
        recognized = []

        for result in results:
            employee_id = result.get("employee_id")

            result["attendance_marked"] = False

            if employee_id:
                result["attendance_marked"] = mark_attendance(
                    employee_id, result.get("confidence", 0.0)
                )
                session_started, session_message = start_attendance_session(
                    employee_id, result.get("confidence", 0.0), "Admin Face Recognition"
                )
                result["session_started"] = session_started
                result["session_message"] = session_message

            # Return employee terminology only
            result.pop("employee_id", None)

            recognized.append(result)

        return jsonify({
            "success": True,
            "results": recognized
        })

    except Exception:
        app.logger.exception("Employee face recognition failed")

        return jsonify({
            "success": False,
            "message": "Face recognition failed. Check the application terminal."
        }), 500


# =========================================================
# ATTENDANCE
# =========================================================


@app.route("/attendance")
def attendance():
    if not admin_required():
        return redirect(url_for("admin_login"))

    search = request.args.get("search", "").strip()
    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()

    connection = get_connection()
    if connection is None:
        return "Database connection failed.", 500

    cursor = connection.cursor(dictionary=True)

    try:
        query = """
            SELECT
                a.id,
                a.employee_id,
                e.name,
                e.department,
                e.designation,
                a.attendance_date,
                a.attendance_time,
                a.status,
                a.confidence,
                a.method
            FROM employee_attendance a
            LEFT JOIN employees e
                ON a.employee_id = e.employee_id
            WHERE 1 = 1
        """
        params = []

        if search:
            query += """
                AND (
                    a.employee_id LIKE %s
                    OR e.name LIKE %s
                    OR e.department LIKE %s
                )
            """
            value = f"%{search}%"
            params.extend([value, value, value])

        if from_date:
            query += " AND a.attendance_date >= %s"
            params.append(from_date)

        if to_date:
            query += " AND a.attendance_date <= %s"
            params.append(to_date)

        query += """
            ORDER BY
                a.attendance_date DESC,
                a.attendance_time DESC
        """

        cursor.execute(query, tuple(params))
        records = cursor.fetchall()

        # Calculate statistics from the same filtered records shown in the table.
        total_records = len(records)
        present_count = sum(
            1 for record in records
            if str(record.get("status", "")).strip().lower() == "present"
        )

        # Count active/approved employees for the Registered Employees card.
        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM employees
            WHERE registration_status = 'Approved'
        """)
        total_employees = cursor.fetchone()["total"]

        # This percentage describes the share of filtered attendance records
        # whose status is Present; it is not a per-employee attendance rate.
        attendance_percentage = (
            round((present_count / total_records) * 100, 1)
            if total_records else 0
        )

        return render_template(
            "attendance.html",
            attendance=records,
            search=search,
            from_date=from_date,
            to_date=to_date,
            total_records=total_records,
            present_count=present_count,
            total_employees=total_employees,
            attendance_percentage=attendance_percentage
        )

    except Exception:
        app.logger.exception("Employee attendance page failed")
        return "Attendance page failed. Check the VS Code terminal.", 500

    finally:
        cursor.close()
        connection.close()



# =========================================================
# ATTENDANCE CSV EXPORT
# =========================================================


@app.route("/attendance/export")
def attendance_export():
    if not admin_required():
        return redirect(url_for("admin_login"))

    search = request.args.get("search", "").strip()
    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()

    connection = get_connection()
    if connection is None:
        return "Database connection failed.", 500

    cursor = connection.cursor(dictionary=True)

    try:
        query = """
            SELECT
                a.employee_id,
                e.name,
                e.department,
                e.designation,
                a.attendance_date,
                a.attendance_time,
                a.status,
                a.confidence,
                a.method
            FROM employee_attendance a
            LEFT JOIN employees e
                ON a.employee_id = e.employee_id
            WHERE 1 = 1
        """
        params = []

        if search:
            query += """
                AND (
                    a.employee_id LIKE %s
                    OR e.name LIKE %s
                    OR e.department LIKE %s
                )
            """
            value = f"%{search}%"
            params.extend([value, value, value])

        if from_date:
            query += " AND a.attendance_date >= %s"
            params.append(from_date)

        if to_date:
            query += " AND a.attendance_date <= %s"
            params.append(to_date)

        query += """
            ORDER BY
                a.attendance_date DESC,
                a.attendance_time DESC
        """

        cursor.execute(query, tuple(params))
        records = cursor.fetchall()

        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow([
            "Employee ID",
            "Name",
            "Department",
            "Designation",
            "Date",
            "Time",
            "Status",
            "Match Score",
            "Method"
        ])

        for record in records:
            confidence = record.get("confidence")
            confidence_text = (
                f"{float(confidence) * 100:.1f}%"
                if confidence is not None else ""
            )

            writer.writerow([
                record.get("employee_id", ""),
                record.get("name", ""),
                record.get("department", ""),
                record.get("designation", ""),
                record.get("attendance_date", ""),
                record.get("attendance_time", ""),
                record.get("status", ""),
                confidence_text,
                record.get("method", "")
            ])

        csv_data = output.getvalue()
        output.close()

        return Response(
            csv_data,
            mimetype="text/csv",
            headers={
                "Content-Disposition":
                    "attachment; filename=employee_attendance_report.csv"
            }
        )

    except Exception:
        app.logger.exception("Employee attendance CSV export failed")
        return "CSV export failed. Check the VS Code terminal.", 500

    finally:
        cursor.close()
        connection.close()



# =========================================================
# FLEXIBLE ATTENDANCE SESSIONS (MULTIPLE IN/OUT PERIODS)
# =========================================================

@app.route("/attendance/sessions", methods=["GET", "POST"])
def attendance_sessions():
    if not admin_required():
        return redirect(url_for("admin_login"))
    message = request.args.get("message", "")
    message_type = request.args.get("type", "info")
    if request.method == "POST":
        action = request.form.get("action", "")
        employee_id = request.form.get("employee_id", "").strip()
        if not employee_id:
            return redirect(url_for("attendance_sessions", message="Choose an employee.", type="error"))
        if action == "check_in":
            daily_saved = mark_attendance(employee_id, None, method='Admin Manual')
            if daily_saved:
                ok, message = start_attendance_session(employee_id, None, "Admin Manual")
            else:
                ok, message = False, "Daily attendance could not be saved; no session was opened."
        elif action == "check_out":
            ok, message = end_attendance_session(employee_id)
        else:
            ok, message = False, "Unknown attendance action."
        if ok:
            write_audit_log("attendance_" + action, "employee", employee_id, message)
        return redirect(url_for("attendance_sessions", message=message, type="success" if ok else "error"))

    connection = get_connection()
    if connection is None:
        return "Database connection failed.", 500
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("""SELECT employee_id, name, department
                          FROM employees
                          WHERE registration_status='Approved' AND is_active=1
                          ORDER BY name""")
        active_employees = cursor.fetchall()
        cursor.execute("""SELECT s.id, s.employee_id, e.name, e.department,
                                 s.work_date, s.check_in_at, s.check_out_at,
                                 s.confidence, s.method
                          FROM employee_attendance_sessions s
                          LEFT JOIN employees e ON e.employee_id=s.employee_id
                          ORDER BY s.check_in_at DESC LIMIT 250""")
        sessions = cursor.fetchall()
        cursor.execute("""SELECT employee_id, check_in_at FROM employee_attendance_sessions
                          WHERE check_out_at IS NULL""")
        open_sessions = {row["employee_id"]: row["check_in_at"] for row in cursor.fetchall()}
        return render_template("attendance_sessions.html", active_employees=active_employees,
                               sessions=sessions, open_sessions=open_sessions,
                               message=message, message_type=message_type)
    except Exception:
        app.logger.exception("Attendance sessions page failed")
        return "Attendance sessions page failed. Run the supplied database migration first.", 500
    finally:
        cursor.close()
        connection.close()


@app.route("/attendance/sessions/export")
def attendance_sessions_export():
    if not admin_required():
        return redirect(url_for("admin_login"))
    connection = get_connection()
    if connection is None:
        return "Database connection failed.", 500
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("""SELECT s.employee_id, e.name, e.department, s.work_date,
                                 s.check_in_at, s.check_out_at, s.method, s.confidence
                          FROM employee_attendance_sessions s
                          LEFT JOIN employees e ON e.employee_id=s.employee_id
                          ORDER BY s.check_in_at DESC""")
        records = cursor.fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Employee ID", "Name", "Department", "Work Date",
                         "Check In", "Check Out", "Method", "Match Score"])
        for row in records:
            writer.writerow([row.get("employee_id"), row.get("name"), row.get("department"),
                             row.get("work_date"), row.get("check_in_at"), row.get("check_out_at"),
                             row.get("method"), row.get("confidence")])
        return Response(output.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=attendance_sessions.csv"})
    except Exception:
        app.logger.exception("Attendance session export failed")
        return "Session export failed.", 500
    finally:
        cursor.close()
        connection.close()


# =========================================================
# REPORTS
# =========================================================


@app.route("/reports")
def reports():
    if not admin_required():
        return redirect(url_for("admin_login"))

    connection = get_connection()
    if connection is None:
        return "Database connection failed.", 500

    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT
                a.attendance_date,
                COUNT(*) AS total_present,
                COUNT(DISTINCT a.employee_id) AS employees_present
            FROM employee_attendance a
            WHERE a.status = 'Present'
            GROUP BY a.attendance_date
            ORDER BY a.attendance_date DESC
        """)
        report_data = cursor.fetchall()

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM employees
            WHERE registration_status = 'Approved' AND is_active = 1
        """)
        total_employees = cursor.fetchone()["total"]

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM employee_attendance
        """)
        total_attendance_records = cursor.fetchone()["total"]

        return render_template(
            "reports.html",
            report_data=report_data,
            total_employees=total_employees,
            total_attendance_records=total_attendance_records
        )

    except Exception:
        app.logger.exception("Employee reports page failed")
        return "Reports page failed. Check the VS Code terminal.", 500

    finally:
        cursor.close()
        connection.close()


# =========================================================
# ADMIN AUDIT LOG VIEW
# =========================================================

@app.route("/admin/audit-logs")
def admin_audit_logs():
    if not admin_required():
        return redirect(url_for("admin_login"))
    connection = get_connection()
    if connection is None:
        return "Database connection failed.", 500
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("""SELECT id, admin_id, admin_email, action, entity_type,
                                 entity_id, details, created_at
                          FROM admin_audit_logs ORDER BY created_at DESC LIMIT 500""")
        logs = cursor.fetchall()
        return render_template("admin_audit_logs.html", logs=logs)
    except Exception:
        app.logger.exception("Audit log page failed")
        return "Audit log page failed. Run the supplied database migration first.", 500
    finally:
        cursor.close()
        connection.close()


# =========================================================
# REPORT EXPORTS (EXCEL AND PDF)
# =========================================================

def _report_rows():
    connection = get_connection()
    if connection is None:
        raise RuntimeError("Database connection failed.")
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("""SELECT a.employee_id, e.name, e.department, e.designation,
                                 a.attendance_date, a.attendance_time, a.status,
                                 a.confidence, a.method
                          FROM employee_attendance a
                          LEFT JOIN employees e ON e.employee_id=a.employee_id
                          ORDER BY a.attendance_date DESC, a.attendance_time DESC""")
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


@app.route("/reports/export.xlsx")
def reports_export_xlsx():
    if not admin_required():
        return redirect(url_for("admin_login"))
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        rows = _report_rows()
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Attendance"
        headers = ["Employee ID", "Name", "Department", "Designation", "Date",
                   "Time", "Status", "Match Score", "Method"]
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="176B4D")
        for row in rows:
            sheet.append([row.get("employee_id"), row.get("name"), row.get("department"),
                          row.get("designation"), str(row.get("attendance_date") or ""),
                          str(row.get("attendance_time") or ""), row.get("status"),
                          row.get("confidence"), row.get("method")])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for column in sheet.columns:
            letter = column[0].column_letter
            sheet.column_dimensions[letter].width = min(
                max(max(len(str(cell.value or "")) for cell in column) + 2, 12), 30)
        output = io.BytesIO()
        workbook.save(output)
        output.seek(0)
        return Response(output.getvalue(),
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": "attachment; filename=facetrack_attendance.xlsx"})
    except Exception:
        app.logger.exception("Excel report export failed")
        return "Excel export failed. Check dependencies and the VS Code terminal.", 500


@app.route("/reports/export.pdf")
def reports_export_pdf():
    if not admin_required():
        return redirect(url_for("admin_login"))
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import landscape, A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        rows = _report_rows()
        output = io.BytesIO()
        doc = SimpleDocTemplate(output, pagesize=landscape(A4),
                                rightMargin=24, leftMargin=24, topMargin=24, bottomMargin=24)
        styles = getSampleStyleSheet()
        story = [Paragraph("FaceTrack Attendance Report", styles["Title"]),
                 Spacer(1, 12)]
        data = [["Employee ID", "Name", "Department", "Designation", "Date", "Time", "Status"]]
        for row in rows:
            data.append([str(row.get("employee_id") or ""), str(row.get("name") or ""),
                         str(row.get("department") or ""), str(row.get("designation") or ""),
                         str(row.get("attendance_date") or ""), str(row.get("attendance_time") or ""),
                         str(row.get("status") or "")])
        table = Table(data, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#176B4D")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#DDE5E1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F8F6")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(table)
        doc.build(story)
        output.seek(0)
        return Response(output.getvalue(), mimetype="application/pdf",
                        headers={"Content-Disposition": "attachment; filename=facetrack_attendance.pdf"})
    except Exception:
        app.logger.exception("PDF report export failed")
        return "PDF export failed. Check dependencies and the VS Code terminal.", 500


# =========================================================
# PUBLIC EMPLOYEE REGISTRATION
# =========================================================

@app.route("/employee/register", methods=["GET", "POST"])
def employee_register():

    # =====================================================
    # GET → ALWAYS SHOW REGISTRATION PAGE
    # =====================================================

    if request.method == "GET":
        return render_template(
            "employee_register.html"
        )

    # =====================================================
    # POST → PROCESS REGISTRATION
    # =====================================================

    employee_id = request.form.get(
        "employee_id",
        ""
    ).strip()

    name = request.form.get(
        "name",
        ""
    ).strip()

    email = request.form.get(
        "email",
        ""
    ).strip()

    department = request.form.get(
        "department",
        ""
    ).strip()

    designation = request.form.get(
        "designation",
        ""
    ).strip()

    phone = request.form.get(
        "phone",
        ""
    ).strip()

    # IMPORTANT:
    # Do NOT use strip() on passwords.
    # Spaces may technically be part of a password.
    password = request.form.get(
        "password",
        ""
    )

    confirm_password = request.form.get(
        "confirm_password",
        ""
    )

    selfie = request.files.get(
        "selfie"
    )

    # =====================================================
    # VALIDATION
    # =====================================================

    if not employee_id:
        return render_template(
            "employee_register.html",
            error="Employee ID is required."
        ), 400

    if not name:
        return render_template(
            "employee_register.html",
            error="Full Name is required."
        ), 400

    if not password:
        return render_template(
            "employee_register.html",
            error="Password is required."
        ), 400

    if len(password) < 6:
        return render_template(
            "employee_register.html",
            error="Password must be at least 6 characters."
        ), 400

    if password != confirm_password:
        return render_template(
            "employee_register.html",
            error="Passwords do not match."
        ), 400

    if not selfie or not selfie.filename:
        return render_template(
            "employee_register.html",
            error="Please capture your selfie before submitting."
        ), 400

    if not allowed_file(selfie.filename):
        return render_template(
            "employee_register.html",
            error="Selfie must be JPG, JPEG or PNG."
        ), 400

    # =====================================================
    # DATABASE CONNECTION
    # =====================================================

    connection = get_connection()

    if connection is None:
        return render_template(
            "employee_register.html",
            error="Database connection failed."
        ), 500

    cursor = connection.cursor(
        dictionary=True
    )

    selfie_path = None

    try:

        # =================================================
        # CHECK IF EMPLOYEE IS ALREADY APPROVED
        # =================================================

        cursor.execute(
            """
            SELECT id
            FROM employees
            WHERE employee_id = %s
            LIMIT 1
            """,
            (employee_id,)
        )

        existing_employee = cursor.fetchone()

        if existing_employee:
            return render_template(
                "employee_register.html",
                error="This Employee ID is already registered."
            ), 409

        # =================================================
        # CHECK PENDING REQUEST
        # =================================================

        cursor.execute(
            """
            SELECT id
            FROM employee_registration_requests
            WHERE employee_id = %s
            AND status = 'Pending'
            LIMIT 1
            """,
            (employee_id,)
        )

        pending_request = cursor.fetchone()

        if pending_request:
            return render_template(
                "employee_register.html",
                error=(
                    "A registration request for this "
                    "Employee ID is already pending."
                )
            ), 409

        # =================================================
        # SAVE SELFIE
        # =================================================

        safe_id = secure_filename(
            employee_id
        )

        extension = (
            selfie.filename
            .rsplit(".", 1)[1]
            .lower()
        )

        filename = (
            f"{safe_id}_selfie."
            f"{extension}"
        )

        selfie_path = os.path.join(
            REGISTRATION_FACE_FOLDER,
            filename
        )

        selfie.save(
            selfie_path
        )

        # =================================================
        # CHECK IMAGE
        # =================================================

        test_image = cv2.imread(
            selfie_path
        )

        if test_image is None:

            if os.path.exists(
                selfie_path
            ):
                os.remove(
                    selfie_path
                )

            selfie_path = None

            return render_template(
                "employee_register.html",
                error=(
                    "The captured selfie could "
                    "not be read."
                )
            ), 400

        # =================================================
        # GENERATE FACE ENCODING
        # =================================================

        face_encoding = get_face_encoding(
            selfie_path
        )

        if face_encoding is None:

            if os.path.exists(
                selfie_path
            ):
                os.remove(
                    selfie_path
                )

            selfie_path = None

            return render_template(
                "employee_register.html",
                error=(
                    "No valid single face found. "
                    "Please capture a clear selfie "
                    "with only one face."
                )
            ), 400

        # =================================================
        # CONVERT FACE ENCODING TO STRING
        # =================================================

        encoding_string = ",".join(
            str(value)
            for value in face_encoding
        )

        # =================================================
        # HASH PASSWORD
        # =================================================

        password_hash = generate_password_hash(
            password
        )

        # =================================================
        # INSERT REGISTRATION REQUEST
        # =================================================

        cursor.execute(
            """
            INSERT INTO employee_registration_requests
            (
                employee_id,
                name,
                email,
                phone,
                department,
                designation,
                selfie_path,
                face_encoding,
                status,
                password_hash
            )
            VALUES
            (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            """,
            (
                employee_id,
                name,
                email,
                phone,
                department,
                designation,
                selfie_path,
                encoding_string,
                "Pending",
                password_hash
            )
        )

        connection.commit()

        # =================================================
        # SUCCESS PAGE
        # =================================================

        return render_template(
            "employee_registration_success.html",
            employee_id=employee_id,
            name=name
        )

    except Exception as error:

        connection.rollback()

        if (
            selfie_path
            and os.path.exists(
                selfie_path
            )
        ):
            os.remove(
                selfie_path
            )

        return render_template(
            "employee_register.html",
            error=f"Registration error: {error}"
        ), 500

    finally:

        cursor.close()
        connection.close()


# =========================================================
# ADMIN REGISTRATION REQUESTS
# =========================================================

@app.route(
    "/admin/registrations"
)
def registration_requests():

    if not admin_required():

        return redirect(
            url_for("admin_login")
        )

    status = request.args.get(
        "status",
        ""
    ).strip()

    connection = get_connection()

    if connection is None:
        return "Database connection failed.", 500

    cursor = connection.cursor(
        dictionary=True
    )

    try:

        if status in [
            "Pending",
            "Approved",
            "Rejected"
        ]:

            cursor.execute(
                """
                SELECT *
                FROM employee_registration_requests
                WHERE status = %s
                ORDER BY created_at DESC
                """,
                (status,)
            )

        else:

            cursor.execute(
                """
                SELECT *
                FROM employee_registration_requests
                ORDER BY created_at DESC
                """
            )

        registrations = cursor.fetchall()

        return render_template(
            "registration_requests.html",
            registrations=registrations,
            selected_status=status
        )

    except Exception as error:

        return (
            f"Registration requests error: {error}",
            500
        )

    finally:

        cursor.close()
        connection.close()


# =========================================================
# REGISTRATION DETAIL
# =========================================================

@app.route(
    "/admin/registrations/<int:request_id>"
)
def registration_detail(
    request_id
):

    if not admin_required():

        return redirect(
            url_for("admin_login")
        )

    connection = get_connection()

    if connection is None:
        return "Database connection failed.", 500

    cursor = connection.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT *
            FROM employee_registration_requests
            WHERE id = %s
            """,
            (request_id,)
        )

        registration = cursor.fetchone()

        if registration is None:

            return (
                "Registration request not found.",
                404
            )

        return render_template(
            "registration_detail.html",
            registration=registration
        )

    finally:

        cursor.close()
        connection.close()


# =========================================================
# APPROVE REGISTRATION
# =========================================================

@app.route(
    "/admin/registrations/<int:request_id>/approve",
    methods=["POST"]
)
def approve_registration(
    request_id
):

    if not admin_required():

        return redirect(
            url_for("admin_login")
        )

    connection = get_connection()

    if connection is None:
        return "Database connection failed.", 500

    cursor = connection.cursor(
        dictionary=True
    )

    try:

        # -------------------------------------------------
        # GET REQUEST
        # -------------------------------------------------

        cursor.execute(
            """
            SELECT *
            FROM employee_registration_requests
            WHERE id = %s
            """,
            (request_id,)
        )

        registration = cursor.fetchone()

        if registration is None:

            return (
                "Registration request not found.",
                404
            )

        # -------------------------------------------------
        # ALREADY APPROVED
        # -------------------------------------------------

        if registration["status"] == "Approved":

            return redirect(
                url_for(
                    "registration_detail",
                    request_id=request_id
                )
            )

        # -------------------------------------------------
        # CHECK EMPLOYEE ID
        # -------------------------------------------------

        cursor.execute(
            """
            SELECT id
            FROM employees
            WHERE employee_id = %s
            LIMIT 1
            """,
            (
                registration["employee_id"],
            )
        )

        if cursor.fetchone():

            return (
                "This Employee ID already exists.",
                409
            )

        # -------------------------------------------------
        # INSERT INTO EMPLOYEES
        # -------------------------------------------------

        cursor.execute(
            """
            INSERT INTO employees
            (
                employee_id,
                name,
                email,
                department,
                designation,
                phone,
                photo_path,
                face_encoding,
                password_hash,
                registration_status,
                approved_at
            )
            VALUES
            (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                'Approved',
                %s
            )
            """,
            (
                registration["employee_id"],
                registration["name"],
                registration["email"],
                registration["department"],
                registration["designation"],
                registration["phone"],
                registration["selfie_path"],
                registration["face_encoding"],
                registration["password_hash"],
                datetime.now()
            )
        )

        # -------------------------------------------------
        # UPDATE REQUEST
        # -------------------------------------------------

        cursor.execute(
            """
            UPDATE employee_registration_requests
            SET
                status = 'Approved',
                reviewed_at = %s
            WHERE id = %s
            """,
            (
                datetime.now(),
                request_id
            )
        )

        connection.commit()
        write_audit_log("registration_approved", "registration_request", request_id,
                        f"Approved employee registration for {employees}")

        return redirect(
            url_for(
                "registration_detail",
                request_id=request_id
            )
        )

    except Exception as error:

        connection.rollback()

        return (
            f"Approval error: {error}",
            500
        )

    finally:

        cursor.close()
        connection.close()


# =========================================================
# REJECT REGISTRATION
# =========================================================

@app.route(
    "/admin/registrations/<int:request_id>/reject",
    methods=["POST"]
)
def reject_registration(
    request_id
):

    if not admin_required():

        return redirect(
            url_for("admin_login")
        )

    reason = request.form.get(
        "rejection_reason",
        ""
    ).strip()

    if not reason:

        return (
            "Please enter a rejection reason.",
            400
        )

    connection = get_connection()

    if connection is None:
        return "Database connection failed.", 500

    cursor = connection.cursor()

    try:

        cursor.execute(
            """
            UPDATE employee_registration_requests
            SET
                status = 'Rejected',
                rejection_reason = %s,
                reviewed_at = %s
            WHERE id = %s
            """,
            (
                reason,
                datetime.now(),
                request_id
            )
        )

        if cursor.rowcount == 0:

            connection.rollback()

            return (
                "Registration request not found.",
                404
            )

        connection.commit()
        write_audit_log(
            "registration_rejected",
            "registration_request",
            request_id,
            f"Rejected employee registration request {request_id}"
        )
        return redirect(
            url_for(
                "registration_detail",
                request_id=request_id
            )
        )

    except Exception as error:

        connection.rollback()

        return (
            f"Rejection error: {error}",
            500
        )

    finally:

        cursor.close()
        connection.close()


# =========================================================
# EMPLOYEE SELFIE FILE
# =========================================================

@app.route(
    "/employee_faces/<path:filename>"
)
def employee_face_file(filename):

    if not admin_required():

        return redirect(
            url_for("admin_login")
        )

    return send_from_directory(
        REGISTRATION_FACE_FOLDER,
        filename
    )


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/health")
def health():

    connection = get_connection()

    if connection:

        connection.close()

        return jsonify({
            "status": "OK",
            "database": "Connected"
        })

    return jsonify({
        "status": "ERROR",
        "database": "Disconnected"
    }), 500



# =========================================================
# RUN APPLICATION
# =========================================================

if __name__ == "__main__":

    print("=" * 60)

    print(
        "FACE RECOGNITION ATTENDANCE SYSTEM"
    )

    print("=" * 60)

    print(
        "Server: http://127.0.0.1:5000"
    )

    print(
        "Admin: "
        "http://127.0.0.1:5000/admin/login"
    )

    print(
        "Employee: "
        "http://127.0.0.1:5000/employee/login"
    )

    print(
        "Registration: "
        "http://127.0.0.1:5000/employee/register"
    )

    print(
        "Employee Attendance: "
        "http://127.0.0.1:5000/employee/recognition"
    )

    print("Admin Leave Management: http://127.0.0.1:5000/admin/leave-management")
    print("Employee Leave Requests: http://127.0.0.1:5000/employee/leave-requests")
    print("=" * 60)

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )