import mysql.connector
from mysql.connector import Error


def create_connection():

    try:

        connection = mysql.connector.connect(
            host="localhost",
            user="root",
            password="916307"
        )

        if connection.is_connected():

            print("MySQL database connected successfully!")

            return connection

    except Error as e:

        print("Error connecting to MySQL:", e)

        return None


# Test connection
connection = create_connection()

if connection:
    connection.close()
    print("Connection closed.")
