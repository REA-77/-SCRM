<#
  bitbrowser-cdp-check.ps1
  ---------------------------------------------------------------
  Purpose: decide whether a BitBrowser window's CDP channel is
           actually usable BEFORE handing it to Playwright/Selenium.

  Why: "the WebSocket connects" does NOT mean "CDP works".
       The browser process can be perfectly healthy while the
       renderer process of some tabs inside the window is deadlocked.
       In that state Runtime.* / Page.* never return, and the
       connection never drops. Playwright's connectOverCDP initialises
       every page target in the window, so a single stuck target makes
       the whole call hang until the outer timeout fires.

  Usage:
       powershell -ExecutionPolicy Bypass -File bitbrowser-cdp-check.ps1
       powershell -ExecutionPolicy Bypass -File bitbrowser-cdp-check.ps1 -Port 62898

  Exit codes: 0 = all targets healthy
              1 = at least one renderer unresponsive
              2 = no CDP debug port found

  NOTE: output is ASCII-only on purpose. Windows PowerShell 5.1 reads
        a .ps1 without BOM as ANSI, which mangles non-ASCII literals.
#>

param(
    [int]$Port = 0,
    [int]$TimeoutMs = 4000,
    [switch]$CloseDead,
    [string]$KeepPattern = ""
)

$ErrorActionPreference = 'Continue'
[System.Net.WebRequest]::DefaultWebProxy = $null

function Get-CdpJson([string]$url) {
    try {
        $req = [System.Net.WebRequest]::Create($url)
        $req.Timeout = $TimeoutMs
        $req.Proxy = $null
        $resp = $req.GetResponse()
        $sr = New-Object System.IO.StreamReader($resp.GetResponseStream(), [Text.Encoding]::UTF8)
        $txt = $sr.ReadToEnd()
        $sr.Close(); $resp.Close()
        return ($txt | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Get-ListeningPorts {
    $set = @{}
    Get-Process -Name BitBrowser -ErrorAction SilentlyContinue | ForEach-Object {
        $owner = $_.Id
        Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.OwningProcess -eq $owner } |
            ForEach-Object { if (-not $set.ContainsKey($_.LocalPort)) { $set[$_.LocalPort] = $true } }
    }
    return $set.Keys
}

function Resolve-CdpPort {
    if ($Port -gt 0) {
        if (Get-CdpJson "http://127.0.0.1:$Port/json/version") { return $Port }
        return -1
    }
    foreach ($p in (Get-ListeningPorts)) {
        $v = Get-CdpJson "http://127.0.0.1:$p/json/version"
        if ($v -and $v.webSocketDebuggerUrl) { return $p }
    }
    return -1
}

$script:ws = $null
$script:ct = [System.Threading.CancellationToken]::None
$script:rxbuf = New-Object byte[] 1048576
$script:recvTask = $null
$script:partial = $null
$script:queue = $null

function Connect-Cdp([string]$wsUrl) {
    $w = New-Object System.Net.WebSockets.ClientWebSocket
    $uri = New-Object System.Uri -ArgumentList @($wsUrl)
    if (-not $w.ConnectAsync($uri, $script:ct).Wait(6000)) { return $false }
    $script:ws = $w
    $script:recvTask = $null
    $script:partial = New-Object System.Text.StringBuilder
    $script:queue = New-Object System.Collections.Queue
    return $true
}

# Never abandon an in-flight ReceiveAsync: a second concurrent call
# throws InvalidOperationException and looks like a server-side drop.
function Pump-Once([int]$ms) {
    if ($null -eq $script:recvTask) {
        $seg = [System.ArraySegment[byte]]::new($script:rxbuf)
        $script:recvTask = $script:ws.ReceiveAsync($seg, $script:ct)
    }
    if (-not $script:recvTask.Wait($ms)) { return $false }
    $rr = $script:recvTask.Result
    $script:recvTask = $null
    [void]$script:partial.Append([Text.Encoding]::UTF8.GetString($script:rxbuf, 0, $rr.Count))
    if ($rr.EndOfMessage) {
        [void]$script:queue.Enqueue($script:partial.ToString())
        $script:partial = New-Object System.Text.StringBuilder
    }
    return $true
}

function Wait-IdMsg([int]$id, [int]$ms) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.ElapsedMilliseconds -lt $ms) {
        while ($script:queue.Count -gt 0) {
            $m = $script:queue.Dequeue()
            if ($m -match ('"id":' + $id + '[,}]')) { return $m }
        }
        $left = $ms - $sw.ElapsedMilliseconds
        if ($left -lt 1) { break }
        try { [void](Pump-Once ([Math]::Min(700, [int]$left))) } catch { return $null }
    }
    return $null
}

function Invoke-Cdp([int]$id, [string]$sessionId, [string]$method, [string]$paramsJson) {
    $msg = '{"id":' + $id + ',' + $(if ($sessionId) { '"sessionId":"' + $sessionId + '",' } else { '' }) + '"method":"' + $method + '"'
    if ($paramsJson) { $msg = $msg + ',"params":' + $paramsJson }
    $msg = $msg + '}'
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $b = [Text.Encoding]::UTF8.GetBytes($msg)
        $seg = [System.ArraySegment[byte]]::new($b)
        if (-not $script:ws.SendAsync($seg, [System.Net.WebSockets.WebSocketMessageType]::Text, $true, $script:ct).Wait(5000)) {
            return @{ ok = $false; ms = -1 }
        }
    } catch {
        return @{ ok = $false; ms = -1 }
    }
    $reply = Wait-IdMsg $id $TimeoutMs
    if ($null -eq $reply) { return @{ ok = $false; ms = $sw.ElapsedMilliseconds } }
    return @{ ok = $true; ms = $sw.ElapsedMilliseconds; msg = $reply }
}

$port = Resolve-CdpPort
if ($port -lt 0) {
    Write-Output "[X] No CDP debug port found. Is a BitBrowser window open?"
    exit 2
}
$base = "http://127.0.0.1:$port"
$ver = Get-CdpJson "$base/json/version"
Write-Output "debug port   : $port"
Write-Output "engine       : $($ver.Browser)"
Write-Output ""

$targets = @(Get-CdpJson "$base/json/list")
$pages = @($targets | Where-Object { $_.type -eq 'page' })
Write-Output ("layer 1  HTTP /json/version   : OK (" + $targets.Count + " targets, " + $pages.Count + " pages)")

if (-not (Connect-Cdp ([string]$ver.webSocketDebuggerUrl))) {
    Write-Output "layer 2  WebSocket handshake  : FAILED"
    exit 1
}
$r = Invoke-Cdp 1 $null 'Browser.getVersion' $null
if (-not $r.ok) {
    Write-Output "layer 2  WebSocket handshake  : connected but no reply"
    exit 1
}
Write-Output ("layer 2  WebSocket handshake  : OK (Browser.getVersion " + $r.ms + "ms)")
Write-Output ""
Write-Output "layer 3  per-page renderer    :"
Write-Output ""

$dead = @()
$i = 100
foreach ($pg in $pages) {
    $i++
    $at = Invoke-Cdp $i $null 'Target.attachToTarget' ('{"targetId":"' + $pg.id + '","flatten":true}')
    $sid = ''
    if ($at.ok -and $at.msg -match '"sessionId":"([^"]+)"') { $sid = $Matches[1] }
    $i++
    $en = Invoke-Cdp $i $sid 'Runtime.enable' $null
    $i++
    $ev = Invoke-Cdp $i $sid 'Runtime.evaluate' '{"expression":"1+1","returnByValue":true}'

    $title = [string]$pg.title
    if ($title.Length -gt 34) { $title = $title.Substring(0, 34) }
    if ($en.ok -and $ev.ok) {
        Write-Output ("  [ OK ]  Runtime.enable {0,5}ms   {1}" -f $en.ms, $title)
    } else {
        Write-Output ("  [DEAD]  no reply after ${TimeoutMs}ms   {0}" -f $title)
        $dead += $pg
    }
}

Write-Output ""
if ($dead.Count -eq 0) {
    if ($CloseDead -and $KeepPattern) {
        foreach ($pg in $pages) {
            if ($pg.url -notmatch $KeepPattern) {
                try {
                    $req = [System.Net.WebRequest]::Create("$base/json/close/$($pg.id)")
                    $req.Timeout = $TimeoutMs
                    $req.Proxy = $null
                    $resp = $req.GetResponse()
                    $resp.Close()
                    Write-Output ("[CLOSED] unrelated page target: " + $pg.url)
                } catch { }
            }
        }
    }
    Write-Output "[OK] every page target is healthy. Safe to hand over to Playwright."
    exit 0
}

Write-Output ("[!] renderer unresponsive in " + $dead.Count + " page target(s):")
foreach ($d in $dead) { Write-Output ("    - " + $d.url) }
Write-Output ""
if ($CloseDead) {
    foreach ($d in $dead) {
        $closed = $false
        try {
            $req = [System.Net.WebRequest]::Create("$base/json/close/$($d.id)")
            $req.Timeout = $TimeoutMs
            $req.Proxy = $null
            $resp = $req.GetResponse()
            $resp.Close()
            $closed = $true
        } catch { }
        if ($closed) {
            Write-Output ("[CLOSED] dead page target: " + $d.url)
        } else {
            Write-Output ("[FAILED] could not close dead page target: " + $d.url)
        }
    }
    if ($KeepPattern) {
        foreach ($pg in $pages) {
            if ($pg.url -notmatch $KeepPattern) {
                $closed = $false
                try {
                    $req = [System.Net.WebRequest]::Create("$base/json/close/$($pg.id)")
                    $req.Timeout = $TimeoutMs
                    $req.Proxy = $null
                    $resp = $req.GetResponse()
                    $resp.Close()
                    $closed = $true
                } catch { }
                if ($closed) { Write-Output ("[CLOSED] unrelated page target: " + $pg.url) }
            }
        }
    }
    Write-Output "Dead page targets were closed. Re-run Playwright CDP connection."
    exit 0
}
Write-Output "connectOverCDP will hang on these. Do one of:"
Write-Output "  A. close those tabs manually in BitBrowser, then re-run this check;"
Write-Output "  B. close the window, fully quit the BitBrowser client, confirm no"
Write-Output "     BitBrowser process is left in Task Manager, then reopen."
exit 1
