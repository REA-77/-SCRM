$ErrorActionPreference = 'Stop'
$projectRoot = 'D:\下载\数据库'
$entryPoint = Join-Path $projectRoot 'app.py'
$pythonPath = 'C:\Users\EDY\AppData\Local\Programs\Python\Python313\python.exe'
$matches = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and $_.CommandLine -like "*$entryPoint*"
})
if ($matches.Count -ne 1) { throw "Expected exactly one project service, found $($matches.Count)" }
$service = $matches[0]
if ($service.ExecutablePath -ne $pythonPath) { throw 'Unexpected service executable' }
$state = Invoke-RestMethod 'http://127.0.0.1:5000/api/analyze/status' -TimeoutSec 10
if ($state.processing) { throw 'Background analysis is active; restart postponed' }
$tasks = Invoke-RestMethod 'http://127.0.0.1:5000/scrm/api/auto-tasks?status=running' -TimeoutSec 10
if ($tasks.total -gt 0) { throw 'Platform writes are active; restart postponed' }
Stop-Process -Id $service.ProcessId
Wait-Process -Id $service.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
$process = Start-Process -FilePath $pythonPath -ArgumentList '-X', 'utf8', ('"' + $entryPoint + '"') -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $projectRoot 'memory-optimized-service.stdout.log') -RedirectStandardError (Join-Path $projectRoot 'memory-optimized-service.stderr.log') -PassThru
Write-Output "Started project service PID $($process.Id)"
