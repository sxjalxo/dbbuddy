# db.py

import mysql.connector

def connect_db(host, user, password, database):
    try:
        conn = mysql.connector.connect(
            host=host,
            user=user,
            password=password,
            database=database
        )
        if conn.is_connected():
            print("[+] Connected to database successfully")
            return conn
        else:
            print("[-] Connection could not be verified: is_connected() returned False")
            return None
    except Exception as e:
        print("[-] Connection failed:", e)
        return None