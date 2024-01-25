import requests

result=requests.get("enter website api url")
user = result.json()
print (user['results'][0]['name'])

