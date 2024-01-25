import requests

result=requests.get("website url")
user = result.json()
name= f"""{user['results'][0]['name']['first']} {user['results'][0]['name']['last']}"""
print(name)
img= f"""{user['results'][0]['picture']}"""
print(img)


