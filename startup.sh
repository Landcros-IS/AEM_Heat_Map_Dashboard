#!/bin/bash
pip install flask flask-cors pyodbc gunicorn
gunicorn --bind=0.0.0.0:8000 --timeout 600 app:app
