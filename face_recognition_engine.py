
import face_recognition
import numpy as np
import cv2

from database import get_connection
from datetime import datetime


# ============================================================
# LOAD REGISTERED EMPLOYEE FACES
# ============================================================

def load_known_faces():
    """
    Load approved employee face encodings from MySQL.

    Returns:
        known_encodings: List of 128-dimensional encodings.
        known_employees: Corresponding employee information.
    """

    connection = get_connection()

    if connection is None:
        print("Database connection failed while loading faces.")
        return [], []

    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT
                id,
                employee_id,
                name,
                department,
                designation,
                face_encoding
            FROM employees
            WHERE face_encoding IS NOT NULL
              AND LENGTH(TRIM(face_encoding)) > 0
              AND registration_status = 'Approved'
        """)

        employees = cursor.fetchall()

        known_encodings = []
        known_employees = []

        for employee in employees:
            try:
                encoding_string = employee["face_encoding"]

                encoding = np.array(
                    [
                        float(value.strip())
                        for value in encoding_string.split(",")
                    ],
                    dtype=np.float64
                )

                if encoding.shape != (128,):
                    print(
                        f"Invalid encoding length for "
                        f"{employee['employee_id']}: {encoding.shape}"
                    )
                    continue

                known_encodings.append(encoding)
                known_employees.append(employee)

                print(
                    f"Loaded employee face: "
                    f"{employee['employee_id']} - "
                    f"{employee['name']}"
                )

            except Exception as error:
                print(
                    f"Could not load encoding for "
                    f"{employee.get('employee_id', 'Unknown')}: {error}"
                )

        print(f"Total employee faces loaded: {len(known_encodings)}")

        return known_encodings, known_employees

    except Exception as error:
        print(f"Error loading employee faces: {error}")
        return [], []

    finally:
        cursor.close()
        connection.close()


# ============================================================
# RECOGNIZE EMPLOYEE FACE
# ============================================================

def recognize_face(frame, tolerance=0.50):
    """
    Recognize employees from an OpenCV BGR webcam frame.

    Returns a list of recognition results.
    """

    try:
        if frame is None:
            print("Received empty camera frame.")
            return []

        # Convert OpenCV BGR image to RGB
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        print(f"Recognition frame size: {rgb_frame.shape}")

        # Detect faces
        face_locations = face_recognition.face_locations(
            rgb_frame,
            model="hog",
            number_of_times_to_upsample=2
        )

        print(f"Faces detected: {len(face_locations)}")

        if not face_locations:
            return []

        # Generate face encodings
        face_encodings = face_recognition.face_encodings(
            rgb_frame,
            known_face_locations=face_locations,
            num_jitters=1
        )

        print(f"Face encodings generated: {len(face_encodings)}")

        # Load registered employee encodings
        known_encodings, known_employees = load_known_faces()

        results = []

        for face_encoding, face_location in zip(
            face_encodings, face_locations
        ):
            result = {
                "name": "Unknown",
                "employee_id": None,
                "department": None,
                "designation": None,
                "confidence": 0.0,
                "distance": None,
                "location": face_location,

                # Temporary compatibility for existing app.py routes.
                # Remove this alias after all routes use employee_id.
                "student_id": None
            }

            if not known_encodings:
                results.append(result)
                continue

            # Calculate distances from registered employee faces
            distances = face_recognition.face_distance(
                known_encodings,
                face_encoding
            )

            best_match_index = int(np.argmin(distances))
            best_distance = float(distances[best_match_index])

            employee = known_employees[best_match_index]

            print(
                f"Best match: {employee['employee_id']} - "
                f"{employee['name']}"
            )
            print(f"Face distance: {best_distance:.4f}")
            print(f"Tolerance: {tolerance:.4f}")

            if best_distance <= tolerance:
                confidence = max(
                    0.0,
                    min(1.0, 1.0 - best_distance)
                )

                result.update({
                    "name": employee["name"],
                    "employee_id": employee["employee_id"],
                    "department": employee["department"],
                    "designation": employee["designation"],
                    "confidence": confidence,
                    "distance": best_distance,

                    # Temporary compatibility with old app.py.
                    "student_id": employee["employee_id"]
                })

                print(
                    f"MATCH FOUND: {employee['name']} "
                    f"({employee['employee_id']})"
                )

            else:
                print("No matching employee found.")

            results.append(result)

        return results

    except Exception as error:
        print(f"Face recognition error: {error}")
        return []


# ============================================================
# MARK EMPLOYEE ATTENDANCE
# ============================================================

def mark_attendance(employee_id, confidence):
    """
    Record employee attendance.

    A unique database constraint ensures one record per employee
    per day. Repeated recognition updates the existing time and
    confidence rather than creating another daily record.
    """

    if not employee_id:
        print("Cannot mark attendance: Employee ID is empty.")
        return False

    connection = get_connection()

    if connection is None:
        print("Database connection failed while marking attendance.")
        return False

    cursor = connection.cursor()

    try:
        now = datetime.now()

        query = """
            INSERT INTO employee_attendance
            (
                employee_id,
                attendance_date,
                attendance_time,
                status,
                confidence,
                method
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                employee_id = employee_id
        """

        values = (
            employee_id,
            now.date(),
            now.time(),
            "Present",
            confidence,
            "Face Recognition"
        )

        cursor.execute(query, values)
        connection.commit()

        print(f"Attendance marked for employee {employee_id}")
        return True

    except Exception as error:
        connection.rollback()
        print(f"Employee attendance error: {error}")
        return False

    finally:
        cursor.close()
        connection.close()
