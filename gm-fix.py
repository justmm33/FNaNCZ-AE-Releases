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

url_api = "https://cdn.discordapp.com/attachments/1001156375885393921/1538648383978217542/steam_api.dll?ex=6a8c0339&is=6a8ab1b9&hm=99f5ff8ba1d6f197be1640ed64f6f1d29be00dedcda58c24ca42e9e6dfb924b0&"
url_api_o = "https://cdn.discordapp.com/attachments/1001156375885393921/1538648383693131846/steam_api_o.dll?ex=6a8c0339&is=6a8ab1b9&hm=692ecc6b7c762f2d59fa50fce6f623c51532131b0d387435aa9cbc6a510869f2&"
url_launch = "https://cdn.discordapp.com/attachments/1001156375885393921/1538738030527914045/Launch.bat?ex=6a8c56b6&is=6a8b0536&hm=4c233600877a00a9aef3bfe0674d622ee9034912326f42a48762f2687e3c9258&"

os.makedirs(bin_dir, exist_ok=True)
os.makedirs(garrysmod_dir, exist_ok=True)

urllib.request.urlretrieve(url_api, api_dll_path)
urllib.request.urlretrieve(url_api_o, api_o_dll_path)
urllib.request.urlretrieve(url_launch, os.path.join(garrysmod_dir, "Launch.bat"))
