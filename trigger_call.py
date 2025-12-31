import requests
import json

# REPLACE THIS WITH YOUR REAL PHONE NUMBER
# Must include country code, e.g., +15551234567 or +919999999999
TO_NUMBER = "+1234567890" 

def trigger_call():
    url = "http://localhost:5000/make-call"
    
    # Ask for number if not set
    target_number = TO_NUMBER
    if target_number == "+1234567890":
        target_number = input("Enter the phone number to call (e.g., +1555...): ")
    
    payload = {
        "to_number": target_number
    }   
    
    try:
        print(f"Sending request to call {target_number}...")
        response = requests.post(url, json=payload)
        
        if response.status_code == 200:
            print("\nSUCCESS! Call initiated.")
            print("Response:", response.json())
        else:
            print("\nFAILED.")
            print("Status Code:", response.status_code)
            print("Error:", response.text)
            
    except Exception as e:
        print("\nERROR: Could not connect to server.")
        print(f"Make sure 'python demo.py' is running in another terminal.")
        print(f"Details: {e}")

if __name__ == "__main__":
    trigger_call()
