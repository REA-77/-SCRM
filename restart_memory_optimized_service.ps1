$ErrorActionPreference = 'Stop'
$projectRoot = 'D:\下载\数据库'
$entryPoint = Join-Path $projectRoot 'app.py'
$pythonPath = 'C:\Users\EDY\AppData\Local\Programs\Python\Python313\python.exe'
$servicePids = @(Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction Stop |
    Select-Object -ExpandProperty OwningProcess -Unique)
$matches = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and
    $servicePids -contains $_.ProcessId -and
    $_.CommandLine -match '(?i)\bapp\.py\b'
})

# 只操作真正监听 5000 的 app.py，兼容从项目目录以相对路径启动的旧实例。
if (-not $matches.Count) { throw 'No Python app.py service is listening on port 5000' }
if (@($matches | Where-Object { $_.ExecutablePath -ne $pythonPath }).Count) {
    throw 'A port-5000 app.py service uses an unexpected Python executable; restart cancelled'
}

$state = Invoke-RestMethod 'http://127.0.0.1:5000/api/analyze/status' -TimeoutSec 10
if ($state.processing) { throw 'Background analysis is active; restart postponed' }
$tasks = Invoke-RestMethod 'http://127.0.0.1:5000/scrm/api/auto-tasks?status=running' -TimeoutSec 10
if ($tasks.total -gt 0) { throw 'Platform writes are active; restart postponed' }

$oldPids = @($matches | Select-Object -ExpandProperty ProcessId)
Stop-Process -Id $oldPids
foreach ($oldProcessId in $oldPids) {
    Wait-Process -Id $oldProcessId -Timeout 10 -ErrorAction SilentlyContinue
}
if (Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 5000 is still occupied after stopping the old app service'
}

$process = Start-Process -FilePath $pythonPath -ArgumentList '-X', 'utf8', ('"' + $entryPoint + '"') -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $projectRoot 'memory-optimized-service.stdout.log') -RedirectStandardError (Join-Path $projectRoot 'memory-optimized-service.stderr.log') -PassThru
for ($attempt = 1; $attempt -le 10; $attempt++) {
    Start-Sleep -Seconds 1
    try {
        $health = Invoke-WebRequest 'http://127.0.0.1:5000/api/analyze/status' -UseBasicParsing -TimeoutSec 3
        if ($health.StatusCode -eq 200) {
            Write-Output "Restarted project service. Stopped: $($oldPids -join ', '); started: $($process.Id)"
            exit 0
        }
    } catch {
        if ($process.HasExited) { throw "New service exited with code $($process.ExitCode)" }
    }
}
throw "New service PID $($process.Id) did not pass its health check within 10 seconds"
