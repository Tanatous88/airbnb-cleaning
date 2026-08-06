#!/usr/bin/env python3
"""Start Guest Greeting & Review Studio:  python run.py"""
import uvicorn

if __name__ == "__main__":
    print("Guest Greeting & Review Studio → http://127.0.0.1:8321")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8321, reload=True)
