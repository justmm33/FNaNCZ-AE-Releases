import os
import urllib.request

base_dir = os.path.dirname(os.path.abspath(__file__))
bin_dir = os.path.join(base_dir, "GarrysMod", "bin")
garrysmod_dir = os.path.join(base_dir, "GarrysMod")

api_dll_path = os.path.join(bin_dir, "steam_api.dll")
api_dll_renamed = os.path.join(bin_dir, "steam_api_.dll")
api_o_dll_path = os.path.join(bin_dir, "steam_api_o.dll")
api_o_dll_renamed = os.path.join(bin_dir, "steam_api_o_.dll")

if os.path.exists(api_dll_path):
    if os.path.exists(api_dll_renamed):
        os.remove(api_dll_renamed)
    os.rename(api_dll_path, api_dll_renamed)

if os.path.exists(api_o_dll_path):
    if os.path.exists(api_o_dll_renamed):
        os.remove(api_o_dll_renamed)
    os.rename(api_o_dll_path, api_o_dll_renamed)

url_api = "https://raw.githubusercontent.com/justmm33/FNaNCZ-AE-Releases/refs/heads/main/steam_api_.dll"
url_api_o = "https://raw.githubusercontent.com/justmm33/FNaNCZ-AE-Releases/refs/heads/main/steam_api_o_.dll"
url_launch = "https://raw.githubusercontent.com/justmm33/FNaNCZ-AE-Releases/refs/heads/main/Launch.bat"

os.makedirs(bin_dir, exist_ok=True)
os.makedirs(garrysmod_dir, exist_ok=True)

urllib.request.urlretrieve(url_api, api_dll_path)
urllib.request.urlretrieve(url_api_o, api_o_dll_path)
urllib.request.urlretrieve(url_launch, os.path.join(garrysmod_dir, "Launch.bat"))
