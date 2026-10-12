"""关掉正在跑的程序（打包版 exe 和源码版 pythonw app.py）。重新打包前先跑一遍。"""

import subprocess

PS = (
    "Get-CimInstance Win32_Process | "
    "Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*app.py*' } | "
    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
)

subprocess.run(["powershell", "-NoProfile", "-Command", PS], capture_output=True)
subprocess.run(["taskkill", "/IM", "锋芒直播录屏.exe", "/F"], capture_output=True)
print("killed")
