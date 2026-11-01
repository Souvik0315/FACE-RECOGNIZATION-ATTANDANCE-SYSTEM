from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response, send_file, send_from_directory

from werkzeug.utils import secure_filename

from database import get_connection

from face_engine import get_face_encoding

from face_recognition_engine import recognize_face, mark_attendance

from datetime import datetime



import os

import base64

import cv2

import numpy as np

import csv

import io











app = Flask(__name__)

app.secret_key = "face-attendance-secret-key"

UPLOAD_FOLDER = "faces"

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# Student ID document folder

ID_DOCUMENT_FOLDER = "student_documents"

os.makedirs(ID_DOCUMENT_FOLDER, exist_ok=True)

ID_DOCUMENT_EXTENSIONS = {

    "jpg",

    "jpeg",

    "png",

    "pdf"

}







# ---------------------------------------------------------



# Helper Functions



# ---------------------------------------------------------







def allowed_file(filename):

    return (

        "." in filename

        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

    )





def allowed_id_document(filename):

    return (

        "." in filename

        and filename.rsplit(".", 1)[1].lower()

        in ID_DOCUMENT_EXTENSIONS

    )









def login_required():



    return "admin_id" in session











# ---------------------------------------------------------



# HOME / LOGIN



# ---------------------------------------------------------







@app.route("/")



def login():



    if "admin_id" in session:



        return redirect(url_for("dashboard"))







    return render_template("login.html")











# ---------------------------------------------------------



# LOGIN PROCESS



# ---------------------------------------------------------







@app.route("/login", methods=["POST"])



def login_process():



    email = request.form.get("email", "").strip()



    password = request.form.get("password", "").strip()







    connection = get_connection()







    if connection is None:



        return "Database connection failed", 500







    cursor = connection.cursor(dictionary=True)







    try:



        cursor.execute(



            """



            SELECT



                id,



                student_id,



                name,



                email,



                course,



                semester,



                phone,



                photo_path,



                face_encoding,



                created_at



            FROM students



            WHERE email = %s



            """,



            (email,)



        )







        admin = cursor.fetchone()







        if admin:



            session["admin_id"] = admin["id"]



            session["admin_name"] = admin["name"]







            return redirect(url_for("dashboard"))







        return render_template(



            "login.html",



            error="Invalid email or password"



        )







    finally:



        cursor.close()



        connection.close()











# ---------------------------------------------------------



# LOGOUT



# ---------------------------------------------------------







@app.route("/logout")



def logout():



    session.clear()







    return redirect(url_for("login"))











# ---------------------------------------------------------



# DASHBOARD



# ---------------------------------------------------------







@app.route("/dashboard")

def dashboard():



    if not login_required():

        return redirect(url_for("login"))



    connection = get_connection()



    if connection is None:

        return "Database connection failed", 500



    cursor = connection.cursor(dictionary=True)



    try:

        cursor.execute("SELECT COUNT(*) AS total FROM students")

        total_students = cursor.fetchone()["total"]



        cursor.execute("""

            SELECT COUNT(*) AS total

            FROM attendance

            WHERE attendance_date = CURDATE()

        """)

        today_attendance = cursor.fetchone()["total"]



        cursor.execute("""

            SELECT COUNT(*) AS total

            FROM students

            WHERE face_encoding IS NOT NULL

              AND LENGTH(TRIM(face_encoding)) > 0

        """)

        registered_faces = cursor.fetchone()["total"]



        if total_students > 0:

            today_attendance_percentage = round(

                (today_attendance / total_students) * 100, 1

            )

        else:

            today_attendance_percentage = 0



        cursor.execute("""

            SELECT

                a.student_id,

                s.name,

                a.attendance_date,

                a.attendance_time,

                a.status,

                a.confidence,

                a.method

            FROM attendance a

            JOIN students s

                ON a.student_id = s.student_id

            ORDER BY a.id DESC

            LIMIT 10

        """)

        recent_attendance = cursor.fetchall()



        return render_template(

            "dashboard.html",

            total_students=total_students,

            today_attendance=today_attendance,

            registered_faces=registered_faces,

            today_attendance_percentage=today_attendance_percentage,

            recent_attendance=recent_attendance

        )



    except Exception as error:

        print(f"Dashboard error: {error}")

        return f"Dashboard error: {error}", 500



    finally:

        cursor.close()

        connection.close()





@app.route("/students")



def students():







    if not login_required():



        return redirect(url_for("login"))







    connection = get_connection()







    if connection is None:



        return "Database connection failed", 500







    cursor = connection.cursor(dictionary=True)







    try:







        cursor.execute(



            """



            SELECT



                id,



                student_id,



                name,



                email,



                course,



                semester,



                phone,



                photo_path,



                face_encoding,



                created_at,







                CASE



                    WHEN face_encoding IS NOT NULL



                         AND LENGTH(TRIM(face_encoding)) > 0



                    THEN 1



                    ELSE 0



                END AS face_registered







            FROM students







            ORDER BY id DESC



            """



        )







        students_list = cursor.fetchall()







        # Debug information



        print("=" * 60)



        print("STUDENT FACE STATUS")



        print("=" * 60)







        for student in students_list:







            print(



                f"Student: {student['student_id']} | "



                f"Name: {student['name']} | "



                f"Face Registered: "



                f"{student['face_registered']}"



            )







        print("=" * 60)







        return render_template(



            "students.html",



            students=students_list



        )







    finally:







        cursor.close()



        connection.close()











# ---------------------------------------------------------



# ADD STUDENT



# ---------------------------------------------------------







@app.route("/students/add", methods=["POST"])



def add_student():



    if not login_required():



        return redirect(url_for("login"))







    # ---------------------------------------------



    # Get student information



    # ---------------------------------------------







    student_id = request.form.get("student_id", "").strip()



    name = request.form.get("name", "").strip()



    email = request.form.get("email", "").strip()



    course = request.form.get("course", "").strip()



    semester = request.form.get("semester", "").strip()



    phone = request.form.get("phone", "").strip()







    photo = request.files.get("photo")







    # ---------------------------------------------



    # Validate basic information



    # ---------------------------------------------







    if not student_id or not name:



        return "Student ID and Name are required.", 400







    if photo is None or not photo.filename:



        return "Please capture a student photo.", 400







    if not allowed_file(photo.filename):



        return "Invalid image format. Use JPG, JPEG or PNG.", 400







    # ---------------------------------------------



    # Create filename



    # ---------------------------------------------







    original_name = secure_filename(photo.filename)







    filename = f"{student_id}_{original_name}"







    full_path = os.path.join(



        app.config["UPLOAD_FOLDER"],



        filename



    )







    # ---------------------------------------------



    # Save captured webcam image



    # ---------------------------------------------







    try:



        photo.save(full_path)







        print("=" * 60)



        print("STUDENT FACE REGISTRATION")



        print("=" * 60)



        print(f"Student ID : {student_id}")



        print(f"Student Name : {name}")



        print(f"Photo Path : {full_path}")







    except Exception as error:



        print(f"Photo save error: {error}")







        return f"Could not save student photo: {error}", 500







    # ---------------------------------------------



    # Verify that the image can be opened



    # ---------------------------------------------







    try:



        test_image = cv2.imread(full_path)







        if test_image is None:



            if os.path.exists(full_path):



                os.remove(full_path)







            return (



                "The captured photo could not be read. "



                "Please capture the photo again.",



                400



            )







        print(f"Image size : {test_image.shape}")







    except Exception as error:



        print(f"Image verification error: {error}")







        if os.path.exists(full_path):



            os.remove(full_path)







        return (



            "Unable to process the captured image.",



            400



        )







    # ---------------------------------------------



    # Generate face encoding



    # ---------------------------------------------







    print("Starting face detection...")







    encoding = get_face_encoding(full_path)







    # ---------------------------------------------



    # Face detection failed



    # ---------------------------------------------







    if encoding is None:



        print("FACE REGISTRATION FAILED")



        print("No valid single face was detected.")







        if os.path.exists(full_path):



            os.remove(full_path)







        return (



            """



            <h2>Face Registration Failed</h2>







            <p>



                No valid single face was found in the captured photo.



            </p>







            <p>Please try again with:</p>







            <ul>



                <li>Good lighting</li>



                <li>Only one person in the frame</li>



                <li>Face directly facing the camera</li>



                <li>Face clearly visible</li>



                <li>Face positioned near the center</li>



            </ul>







            <br>







            <a href="/students">



                ← Back to Student Registration



            </a>



            """,



            400



        )







    # ---------------------------------------------



    # Face encoding successful



    # ---------------------------------------------







    print("FACE DETECTED SUCCESSFULLY")



    print(f"Encoding length : {len(encoding)}")







    face_encoding = ",".join(



        str(value)



        for value in encoding



    )







    # ---------------------------------------------



    # Connect to database



    # ---------------------------------------------







    connection = get_connection()







    if connection is None:



        if os.path.exists(full_path):



            os.remove(full_path)







        return "Database connection failed.", 500







    cursor = connection.cursor()







    try:



        # -----------------------------------------



        # Check duplicate Student ID



        # -----------------------------------------







        cursor.execute(



            """



            SELECT id



            FROM students



            WHERE student_id = %s



            """,



            (student_id,)



        )







        existing_student = cursor.fetchone()







        if existing_student:



            connection.close()







            if os.path.exists(full_path):



                os.remove(full_path)







            return (



                f"""



                <h2>Student ID Already Exists</h2>







                <p>



                    Student ID <strong>{student_id}</strong>



                    is already registered.



                </p>







                <a href="/students">



                    ← Back to Students



                </a>



                """,



                400



            )







        # -----------------------------------------



        # Insert student



        # -----------------------------------------







        cursor.execute(



            """



            INSERT INTO students



            (



                student_id,



                name,



                email,



                course,



                semester,



                phone,



                photo_path,



                face_encoding



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



                %s



            )



            """,



            (



                student_id,



                name,



                email,



                course,



                semester,



                phone,



                full_path,



                face_encoding



            )



        )







        connection.commit()







        print("=" * 60)



        print("STUDENT REGISTERED SUCCESSFULLY")



        print(f"Student ID : {student_id}")



        print(f"Name       : {name}")



        print("=" * 60)







    except Exception as error:



        connection.rollback()







        print(f"Database error: {error}")







        if os.path.exists(full_path):



            os.remove(full_path)







        return (



            f"Error adding student: {error}",



            400



        )







    finally:



        cursor.close()



        connection.close()







    return redirect(url_for("students"))











# ---------------------------------------------------------



# DELETE STUDENT



# ---------------------------------------------------------







@app.route("/students/delete/<int:student_pk>")



def delete_student(student_pk):



    if not login_required():



        return redirect(url_for("login"))







    connection = get_connection()







    if connection is None:



        return "Database connection failed", 500







    cursor = connection.cursor()







    try:



        cursor.execute(



            "DELETE FROM students WHERE id = %s",



            (student_pk,)



        )







        connection.commit()







    finally:



        cursor.close()



        connection.close()







    return redirect(url_for("students"))











# ---------------------------------------------------------



# RECOGNITION PAGE



# ---------------------------------------------------------







@app.route("/recognition")



def recognition():



    if not login_required():



        return redirect(url_for("login"))







    return render_template("recognition.html")











# ---------------------------------------------------------



# FACE RECOGNITION API



# ---------------------------------------------------------







@app.route("/api/recognize", methods=["POST"])



def api_recognize():



    if not login_required():



        return jsonify({



            "success": False,



            "message": "Unauthorized"



        }), 401







    data = request.get_json()







    if not data or "image" not in data:



        return jsonify({



            "success": False,



            "message": "No image received"



        }), 400







    try:



        image_data = data["image"]







        if "," in image_data:



            image_data = image_data.split(",", 1)[1]







        image_bytes = base64.b64decode(image_data)







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



            if result["student_id"]:



                marked = mark_attendance(



                    result["student_id"],



                    result["confidence"]



                )







                result["attendance_marked"] = marked







            recognized.append(result)







        return jsonify({



            "success": True,



            "results": recognized



        })







    except Exception as error:



        return jsonify({



            "success": False,



            "message": str(error)



        }), 500











# ---------------------------------------------------------



# ATTENDANCE



# ---------------------------------------------------------







@app.route("/attendance")

def attendance():



    if not login_required():

        return redirect(url_for("login"))



    search = request.args.get("search", "").strip()

    from_date = request.args.get("from_date", "").strip()

    to_date = request.args.get("to_date", "").strip()



    connection = get_connection()



    if connection is None:

        return "Database connection failed", 500



    cursor = connection.cursor(dictionary=True)



    try:

        query = """

            SELECT

                a.id,

                a.student_id,

                s.name,

                s.course,

                s.semester,

                a.attendance_date,

                a.attendance_time,

                a.status,

                a.confidence,

                a.method

            FROM attendance a

            JOIN students s

                ON a.student_id = s.student_id

            WHERE 1 = 1

        """



        params = []



        if search:

            query += """

                AND (

                    a.student_id LIKE %s

                    OR s.name LIKE %s

                )

            """

            search_value = f"%{search}%"

            params.extend([search_value, search_value])



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



        total_records = len(records)



        present_count = sum(

            1 for record in records

            if str(record.get("status", "")).lower() == "present"

        )



        cursor.execute("SELECT COUNT(*) AS total FROM students")

        total_students = cursor.fetchone()["total"]



        if from_date and to_date:

            try:

                start_date = datetime.strptime(from_date, "%Y-%m-%d").date()

                end_date = datetime.strptime(to_date, "%Y-%m-%d").date()

                total_days = max((end_date - start_date).days + 1, 1)

            except ValueError:

                total_days = 1

        elif from_date or to_date:

            total_days = 1

        else:

            distinct_dates = {

                record["attendance_date"]

                for record in records

                if record.get("attendance_date") is not None

            }

            total_days = max(len(distinct_dates), 1)



        possible_attendance = total_students * total_days



        if possible_attendance > 0:

            attendance_percentage = round(

                (present_count / possible_attendance) * 100, 1

            )

        else:

            attendance_percentage = 0



        return render_template(

            "attendance.html",

            attendance=records,

            search=search,

            from_date=from_date,

            to_date=to_date,

            total_records=total_records,

            present_count=present_count,

            total_students=total_students,

            attendance_percentage=attendance_percentage

        )



    except Exception as error:

        print(f"Attendance page error: {error}")

        return f"Attendance error: {error}", 500



    finally:

        cursor.close()

        connection.close()





# ---------------------------------------------------------

# ATTENDANCE CSV EXPORT

# ---------------------------------------------------------



@app.route("/attendance/export")

def attendance_export():



    if not login_required():

        return redirect(url_for("login"))



    search = request.args.get("search", "").strip()

    from_date = request.args.get("from_date", "").strip()

    to_date = request.args.get("to_date", "").strip()



    connection = get_connection()



    if connection is None:

        return "Database connection failed", 500



    cursor = connection.cursor(dictionary=True)



    try:

        query = """

            SELECT

                a.student_id,

                s.name,

                s.course,

                s.semester,

                a.attendance_date,

                a.attendance_time,

                a.status,

                a.confidence,

                a.method

            FROM attendance a

            JOIN students s

                ON a.student_id = s.student_id

            WHERE 1 = 1

        """



        params = []



        if search:

            query += """

                AND (

                    a.student_id LIKE %s

                    OR s.name LIKE %s

                )

            """

            search_value = f"%{search}%"

            params.extend([search_value, search_value])



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

            "Student ID",

            "Name",

            "Course",

            "Semester",

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

                if confidence is not None

                else ""

            )



            writer.writerow([

                record.get("student_id", ""),

                record.get("name", ""),

                record.get("course", ""),

                record.get("semester", ""),

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

                    "attachment; filename=attendance_report.csv"

            }

        )



    except Exception as error:

        print(f"CSV export error: {error}")

        return f"CSV export error: {error}", 500



    finally:

        cursor.close()

        connection.close()





@app.route("/reports")

def reports():



    if not login_required():

        return redirect(url_for("login"))



    connection = get_connection()



    if connection is None:

        return "Database connection failed", 500



    cursor = connection.cursor(dictionary=True)



    try:

        cursor.execute("""

            SELECT

                a.attendance_date,

                COUNT(*) AS total_present,

                COUNT(DISTINCT a.student_id) AS students_present

            FROM attendance a

            GROUP BY a.attendance_date

            ORDER BY a.attendance_date DESC

        """)

        report_data = cursor.fetchall()



        cursor.execute("SELECT COUNT(*) AS total FROM students")

        total_students = cursor.fetchone()["total"]



        cursor.execute("SELECT COUNT(*) AS total FROM attendance")

        total_attendance_records = cursor.fetchone()["total"]



        return render_template(

            "reports.html",

            reports=report_data,

            total_students=total_students,

            total_attendance_records=total_attendance_records

        )



    except Exception as error:

        print(f"Reports error: {error}")

        return f"Reports error: {error}", 500



    finally:

        cursor.close()

        connection.close()





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











# ---------------------------------------------------------



# RUN APPLICATION



# ---------------------------------------------------------








# ---------------------------------------------------------
# PUBLIC STUDENT REGISTRATION + ADMIN APPROVAL
# ---------------------------------------------------------

REGISTRATION_FACE_FOLDER = "student_faces"
os.makedirs(REGISTRATION_FACE_FOLDER, exist_ok=True)


def ensure_registration_table():
    connection = get_connection()
    if connection is None:
        return
    cursor = connection.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS student_registration_requests (
                id INT AUTO_INCREMENT PRIMARY KEY,
                student_id VARCHAR(100) NOT NULL,
                name VARCHAR(255) NOT NULL,
                email VARCHAR(255), course VARCHAR(255), semester VARCHAR(100), phone VARCHAR(50),
                selfie_path VARCHAR(500) NOT NULL,
                id_document_path VARCHAR(500) NULL,
                face_encoding LONGTEXT,
                status VARCHAR(30) NOT NULL DEFAULT 'Pending',
                rejection_reason TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                reviewed_at DATETIME NULL,
                UNIQUE KEY uq_registration_student_id (student_id)
            )
        """)
        connection.commit()
    except Exception as error:
        connection.rollback()
        print(f"Registration table setup error: {error}")
    finally:
        cursor.close()
        connection.close()


ensure_registration_table()


@app.route("/student/register", methods=["GET", "POST"])
def student_register():
    if request.method == "GET":
        return render_template("student_register.html")

    student_id = request.form.get("student_id", "").strip()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    course = request.form.get("course", "").strip()
    semester = request.form.get("semester", "").strip()
    phone = request.form.get("phone", "").strip()
    selfie = request.files.get("selfie") or request.files.get("photo")

    if not student_id:
        return "Student ID is required.", 400

    if not name:
        return "Name is required.", 400

    if not selfie or not selfie.filename:
        return "Please capture/upload your selfie.", 400

    if not allowed_file(selfie.filename):
        return "Selfie must be JPG, JPEG or PNG.", 400

    connection = get_connection()
    if connection is None:
        return "Database connection failed.", 500

    cursor = connection.cursor(dictionary=True)
    selfie_path = None

    try:
        cursor.execute(
            "SELECT id FROM students WHERE student_id=%s LIMIT 1",
            (student_id,)
        )
        if cursor.fetchone():
            return "This Student ID is already registered.", 409

        cursor.execute(
            """
            SELECT id FROM student_registration_requests
            WHERE student_id=%s AND status IN ('Pending','Approved')
            LIMIT 1
            """,
            (student_id,)
        )
        if cursor.fetchone():
            return "A registration request for this Student ID already exists.", 409

        safe_id = secure_filename(student_id)
        selfie_ext = selfie.filename.rsplit(".", 1)[1].lower()
        selfie_path = os.path.join(
            REGISTRATION_FACE_FOLDER,
            f"{safe_id}_selfie.{selfie_ext}"
        )

        selfie.save(selfie_path)

        encoding = get_face_encoding(selfie_path)
        if encoding is None:
            if os.path.exists(selfie_path):
                os.remove(selfie_path)
            return (
                "No valid single face found. Please submit a clear selfie with only one face.",
                400
            )

        face_encoding = ",".join(str(value) for value in encoding)

        cursor.execute(
            """
            INSERT INTO student_registration_requests
            (
                student_id,
                name,
                email,
                course,
                semester,
                phone,
                selfie_path,
                id_document_path,
                face_encoding,
                status
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,%s,'Pending')
            """,
            (
                student_id,
                name,
                email,
                course,
                semester,
                phone,
                selfie_path,
                face_encoding
            )
        )

        connection.commit()

        return render_template(
            "student_registration_success.html",
            student_id=student_id,
            name=name
        )

    except Exception as error:
        connection.rollback()
        print(f"Student registration error: {error}")
        if selfie_path and os.path.exists(selfie_path):
            os.remove(selfie_path)
        return f"Registration error: {error}", 500

    finally:
        cursor.close()
        connection.close()


@app.route("/student_faces/<path:filename>")
def student_face_file(filename):
    return send_from_directory(REGISTRATION_FACE_FOLDER, filename)


@app.route("/student_documents/<path:filename>")
def student_document_file(filename):
    if not login_required():
        return redirect(url_for("login"))
    return send_from_directory(ID_DOCUMENT_FOLDER, filename)


@app.route("/admin/registrations")
def registration_requests():
    if not login_required():
        return redirect(url_for("login"))

    connection = get_connection()
    if connection is None:
        return "Database connection failed", 500
    cursor = connection.cursor(dictionary=True)
    try:
        status = request.args.get("status", "Pending").strip()
        if status not in {"Pending", "Approved", "Rejected", "All"}:
            status = "Pending"

        query = """
            SELECT id,student_id,name,email,course,semester,phone,status,
                   rejection_reason,created_at,reviewed_at
            FROM student_registration_requests
        """
        if status == "All":
            cursor.execute(query + " ORDER BY created_at DESC")
        else:
            cursor.execute(query + " WHERE status=%s ORDER BY created_at DESC", (status,))

        registrations = cursor.fetchall()
        return render_template("registration_requests.html",
                               registrations=registrations,
                               selected_status=status)
    finally:
        cursor.close()
        connection.close()


@app.route("/admin/registrations/<int:request_id>")
def registration_detail(request_id):
    if not login_required():
        return redirect(url_for("login"))

    connection = get_connection()
    if connection is None:
        return "Database connection failed", 500
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM student_registration_requests WHERE id=%s", (request_id,))
        registration = cursor.fetchone()
        if registration is None:
            return "Registration request not found.", 404
        return render_template("registration_detail.html", registration=registration)
    finally:
        cursor.close()
        connection.close()


@app.route("/admin/registrations/<int:request_id>/approve", methods=["POST"])
def approve_registration(request_id):
    if not login_required():
        return redirect(url_for("login"))

    connection = get_connection()
    if connection is None:
        return "Database connection failed", 500
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM student_registration_requests WHERE id=%s FOR UPDATE", (request_id,))
        registration = cursor.fetchone()
        if registration is None:
            return "Registration request not found.", 404

        if registration["status"] == "Approved":
            return redirect(url_for("registration_detail", request_id=request_id))

        cursor.execute("SELECT id FROM students WHERE student_id=%s LIMIT 1", (registration["student_id"],))
        if cursor.fetchone():
            return "This Student ID already exists in students.", 409

        encoding = registration.get("face_encoding")
        if not encoding:
            values = get_face_encoding(registration["selfie_path"])
            if values is None:
                return "The selfie does not contain a valid single face.", 400
            encoding = ",".join(str(value) for value in values)

        cursor.execute("""
            INSERT INTO students
            (student_id,name,email,course,semester,phone,photo_path,face_encoding)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (registration["student_id"],registration["name"],registration["email"],
              registration["course"],registration["semester"],registration["phone"],
              registration["selfie_path"],encoding))

        cursor.execute("""
            UPDATE student_registration_requests
            SET status='Approved',reviewed_at=%s,rejection_reason=NULL
            WHERE id=%s
        """, (datetime.now(), request_id))

        connection.commit()
        return redirect(url_for("registration_detail", request_id=request_id))
    except Exception as error:
        connection.rollback()
        print(f"Approve registration error: {error}")
        return f"Approval error: {error}", 500
    finally:
        cursor.close()
        connection.close()


@app.route("/admin/registrations/<int:request_id>/reject", methods=["POST"])
def reject_registration(request_id):
    if not login_required():
        return redirect(url_for("login"))

    reason = request.form.get("reason", "Rejected by admin").strip() or "Rejected by admin"
    connection = get_connection()
    if connection is None:
        return "Database connection failed", 500
    cursor = connection.cursor()
    try:
        cursor.execute("""
            UPDATE student_registration_requests
            SET status='Rejected',rejection_reason=%s,reviewed_at=%s
            WHERE id=%s
        """, (reason, datetime.now(), request_id))
        connection.commit()
        return redirect(url_for("registration_detail", request_id=request_id))
    except Exception as error:
        connection.rollback()
        return f"Rejection error: {error}", 500
    finally:
        cursor.close()
        connection.close()
@app.route("/student_faces/<path:filename>")
def student_face_file(filename):

    if "admin_id" not in session:
        return redirect(url_for("login"))

    return send_from_directory(
        REGISTRATION_FACE_FOLDER,
        filename
    )

if __name__ == "__main__":



    print("=" * 50)



    print("FACE RECOGNITION ATTENDANCE SYSTEM")



    print("=" * 50)



    print("Server: http\\://127.0.0.1:5000")



    print("Login: admin@example.com")



    print("Password: admin123")



    print("=" * 50)







    app.run(



        host="127.0.0.1",



        port=5000,



        debug=True



    )