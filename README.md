
# FACETRACK — Face Recognition Attendance System

FACETRACK is a face-recognition-based attendance management system developed using Python, Flask, and MySQL.

## Features

- Employee registration and management
- Face recognition
- Attendance tracking
- Admin dashboard
- Employee login and profile management
- Leave request management
- Attendance reports

## Technologies Used

- Python
- Flask
- MySQL
- HTML
- CSS
- JavaScript
- OpenCV and face-recognition libraries

## Project Structure

- `app.py` — Main Flask application
- `templates/` — HTML pages
- `config.py` — Application configuration
- `database.py` — Database connection and operations
- `face_engine.py` — Face recognition functionality
- `requirements.txt` — Python dependencies

## Setup Instructions

1. Install Python and MySQL.
2. Clone this repository.
3. Create and activate a Python virtual environment.
4. Install dependencies:

   `pip install -r requirements.txt`

5. Configure your local database credentials and Flask secret key using environment variables.
6. Create the required database tables.
7. Start the application:

   `python app.py`

## Security

Do not upload real employee photos, face encodings, database passwords, secret keys, or private employee information.

## Project Status

Under development.
