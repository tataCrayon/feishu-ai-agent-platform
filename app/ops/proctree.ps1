Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'nacos|claude|cc-connect|npx|cmd|node' } | ForEach-Object {
    $cmd = ''
    if ($_.CommandLine -and $_.CommandLine.Length -gt 200) { $cmd = $_.CommandLine.Substring(0, 200) } elseif ($_.CommandLine) { $cmd = $_.CommandLine }
    Write-Output ("{0}`tPPID={1}`t{2}`t{3}" -f $_.ProcessId, $_.ParentProcessId, $_.Name, $cmd)
} | Sort-Object