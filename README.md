
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
- OpenCV
- Face Recognition

## Project Structure

- `app.py` — Main Flask application
- `templates/` — HTML templates
- `config.py` — Application configuration
- `database.py` — Database operations
- `face_engine.py` — Face recognition functionality
- `requirements.txt` — Python dependencies

## Installation

1. Install Python and MySQL.
2. Clone this repository:

   `git clone https://github.com/Souvik0315/FACE-RECOGNIZATION-ATTANDANCE-SYSTEM.git`

3. Open the project folder:

   `cd FACE-RECOGNIZATION-ATTANDANCE-SYSTEM`

4. Create a virtual environment:

   `python -m venv venv`

5. Activate it on Windows:

   `venv\Scripts\activate`

6. Install dependencies:

   `pip install -r requirements.txt`

7. Configure your local database credentials and Flask secret key using environment variables.
8. Create the required MySQL database and tables.
9. Run the application:

   `python app.py`

## Security

Never commit passwords, secret keys, real employee photos, face encodings, or private employee records.

## Project Status

Under development.
