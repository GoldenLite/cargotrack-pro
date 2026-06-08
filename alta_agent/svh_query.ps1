# SVH MS SQL query helper for alta_agent (called via subprocess from Python).
#
# Operations:
#   --Op list  --SinceDays N --Types T1,T2[,...] [--CustomsCode XX]
#       Lists incoming envelopes in window. Outputs JSON Lines:
#       {"envelope_id":..., "msg_type":..., "prepared_at":..., "customs_code":..., "document_id":..., "ref_document_id":...}
#
#   --Op fetch --EnvelopeIds GUID1,GUID2,...
#       Fetches raw XML for given envelopes (batch — single SQL roundtrip).
#       Outputs JSON Lines: {"envelope_id":..., "msg_type":..., "prepared_at":..., "customs_code":..., "raw_xml_b64":..., "ok": true|false}
#
# Forces stdout UTF-8 (default on RU Windows is cp866 which corrupts ASCII base64 in edge cases).
# Does NOT use Add-Type — System.Data.SqlClient is auto-resolved by fully-qualified name on PS 5.1.

param(
    [Parameter(Mandatory=$true)][string]$Op,
    [string]$IniPath = 'C:\ALTA\IN\alta_agent\alta_agent.ini',
    [int]$SinceDays = 7,
    [string]$Types = 'CMN.13010,CMN.13029,CMN.13014,CMN.13021',
    [string]$CustomsCode = '',
    [string]$EnvelopeIds = '',
    [int]$QueryTimeoutSec = 30
)

# Force UTF-8 stdout — review concern #9
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Read-Ini {
    param([string]$Path)
    if (-not (Test-Path $Path)) { throw "Not found: $Path" }
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $text = $null
    foreach ($enc in @('UTF-8', 'Windows-1251')) {
        try {
            $text = [System.Text.Encoding]::GetEncoding($enc).GetString($bytes)
            if ($text -match '^\s*\[') { break }
        } catch { continue }
    }
    if (-not $text) { throw "Cannot decode $Path" }
    $ini = @{}; $section = ''
    foreach ($line in $text -split "`r?`n") {
        $line = $line.Trim()
        if (-not $line -or $line.StartsWith('#') -or $line.StartsWith(';')) { continue }
        if ($line -match '^\[([^\]]+)\]$') {
            $section = $matches[1].Trim()
            if (-not $ini.ContainsKey($section)) { $ini[$section] = @{} }
            continue
        }
        if ($line -match '^([^=]+)=(.*)$' -and $section) {
            $ini[$section][$matches[1].Trim()] = $matches[2].Trim()
        }
    }
    return $ini
}

function New-SqlConnection {
    param([hashtable]$Cfg)
    $server = $Cfg['db_host']
    $port = if ($Cfg['db_port']) { $Cfg['db_port'] } else { '1433' }
    $dbName = $Cfg['db_name']
    $user = $Cfg['db_user']
    $pass = $Cfg['db_password']
    $connStr = "Server=$server,$port;Database=$dbName;User Id=$user;Password=$pass;TrustServerCertificate=true;Encrypt=false;Connect Timeout=10"
    $conn = New-Object System.Data.SqlClient.SqlConnection $connStr
    $conn.Open()
    return $conn
}

function Decode-MsgBytes {
    # Returns string OR $null. Auto-detects gzip via magic bytes 1f 8b.
    # Review fix #3: reset position before each attempt.
    param([byte[]]$Bytes)
    if (-not $Bytes -or $Bytes.Length -eq 0) { return $null }
    $isGzip = ($Bytes.Length -ge 2 -and $Bytes[0] -eq 0x1f -and $Bytes[1] -eq 0x8b)
    if ($isGzip) {
        try {
            $msIn = New-Object System.IO.MemoryStream(,$Bytes)
            $gz = New-Object System.IO.Compression.GZipStream($msIn, [System.IO.Compression.CompressionMode]::Decompress)
            $rdr = New-Object System.IO.StreamReader($gz, [System.Text.Encoding]::UTF8)
            $txt = $rdr.ReadToEnd()
            $rdr.Close(); $gz.Close(); $msIn.Close()
            return $txt
        } catch {
            return $null
        }
    }
    # Plain text — try UTF-8 then Windows-1251
    foreach ($enc in @('UTF-8', 'Windows-1251')) {
        try {
            $txt = [System.Text.Encoding]::GetEncoding($enc).GetString($Bytes)
            if ($txt -match '<\?xml|<env:Envelope|<edcnt:|<do1r:|<dori:|<whdi:|<whgou:') {
                return $txt
            }
        } catch { continue }
    }
    return $null
}

# Main entry
$ini = Read-Ini -Path $IniPath
if (-not $ini.ContainsKey('db_reconcile_svh')) {
    Write-Error "Section [db_reconcile_svh] not found in $IniPath"
    exit 1
}
$cfg = $ini['db_reconcile_svh']

try {
    $conn = New-SqlConnection -Cfg $cfg
} catch {
    Write-Error "MS SQL connection failed: $_"
    exit 2
}

try {
    switch ($Op.ToLower()) {
        'list' {
            # Review fix #2: negate SinceDays in PowerShell, SQL uses +@win
            $negDays = -[int]$SinceDays
            $typesList = $Types -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
            # Build IN clause with placeholders @t0, @t1, ...
            $placeholders = @()
            for ($i = 0; $i -lt $typesList.Count; $i++) { $placeholders += "@t$i" }
            $inClause = ($placeholders -join ',')
            $sql = "SELECT EnvelopeID, MessageType, PreparationDateTime, InOutDateTime, CustomsCode, DocumentID, RefDocumentID FROM ED2Msgs WHERE Incoming = 1 AND InOutDateTime > DATEADD(day, @win, GETDATE()) AND MessageType IN ($inClause)"
            if ($CustomsCode) { $sql += " AND CustomsCode = @cc" }
            $cmd = $conn.CreateCommand()
            $cmd.CommandText = $sql
            $cmd.CommandTimeout = $QueryTimeoutSec
            [void]$cmd.Parameters.AddWithValue('@win', $negDays)
            for ($i = 0; $i -lt $typesList.Count; $i++) {
                [void]$cmd.Parameters.AddWithValue("@t$i", $typesList[$i])
            }
            if ($CustomsCode) { [void]$cmd.Parameters.AddWithValue('@cc', $CustomsCode) }
            $r = $cmd.ExecuteReader()
            $count = 0
            while ($r.Read()) {
                $count++
                $obj = [PSCustomObject]@{
                    envelope_id = "$($r['EnvelopeID'])"
                    msg_type = "$($r['MessageType'])"
                    prepared_at = ([datetime]$r['PreparationDateTime']).ToString('yyyy-MM-ddTHH:mm:ss')
                    in_out_at = ([datetime]$r['InOutDateTime']).ToString('yyyy-MM-ddTHH:mm:ss')
                    customs_code = "$($r['CustomsCode'])"
                    document_id = "$($r['DocumentID'])"
                    ref_document_id = "$($r['RefDocumentID'])"
                }
                $obj | ConvertTo-Json -Compress
            }
            $r.Close()
            [Console]::Error.WriteLine("svh_query list: $count rows")
        }
        'fetch' {
            if (-not $EnvelopeIds) {
                Write-Error '--EnvelopeIds is required for op=fetch'
                exit 3
            }
            # Review fix #8: BATCH fetch — single SQL roundtrip for all envelopes
            # (avoids subprocess-per-envelope cold-start overhead of 300-700ms × N).
            $envList = $EnvelopeIds -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
            if ($envList.Count -eq 0) { exit 0 }
            $placeholders = @()
            for ($i = 0; $i -lt $envList.Count; $i++) { $placeholders += "@e$i" }
            $inClause = ($placeholders -join ',')
            $sql = "SELECT EnvelopeID, MessageType, PreparationDateTime, CustomsCode, DocumentID, RefDocumentID, Msg FROM ED2Msgs WHERE EnvelopeID IN ($inClause)"
            $cmd = $conn.CreateCommand()
            $cmd.CommandText = $sql
            $cmd.CommandTimeout = $QueryTimeoutSec
            for ($i = 0; $i -lt $envList.Count; $i++) {
                [void]$cmd.Parameters.AddWithValue("@e$i", $envList[$i])
            }
            $r = $cmd.ExecuteReader()
            while ($r.Read()) {
                $env = "$($r['EnvelopeID'])"
                $bytes = $null
                if ($r['Msg'] -ne [DBNull]::Value) { $bytes = [byte[]]$r['Msg'] }
                $xml = Decode-MsgBytes -Bytes $bytes
                if (-not $xml) {
                    $obj = [PSCustomObject]@{
                        envelope_id = $env
                        ok = $false
                        error = 'decode_failed'
                    }
                    $obj | ConvertTo-Json -Compress
                    continue
                }
                $b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($xml))
                $obj = [PSCustomObject]@{
                    envelope_id = $env
                    msg_type = "$($r['MessageType'])"
                    prepared_at = ([datetime]$r['PreparationDateTime']).ToString('yyyy-MM-ddTHH:mm:ss')
                    customs_code = "$($r['CustomsCode'])"
                    document_id = "$($r['DocumentID'])"
                    ref_document_id = "$($r['RefDocumentID'])"
                    raw_xml_b64 = $b64
                    ok = $true
                }
                $obj | ConvertTo-Json -Compress
            }
            $r.Close()
        }
        'list-do1' {
            # Tянем parsed-таблицу ED2WHDocInventory — там Декларант наполняет
            # данные ДО1 регистрации напрямую (CMN.13010 в ED2Msgs может
            # отсутствовать). Это закрывает gap который видно по 425-10390671.
            $negDays = -[int]$SinceDays
            $sql = "SELECT DocumentID, InDate, MsgDate, DO1DatePresent, nreg_tamnum, td, ntsd, nlic, ncar, reg_id, main_id FROM ED2WHDocInventory WHERE InDate > DATEADD(day, @win, GETDATE()) AND nreg_tamnum IS NOT NULL AND nreg_tamnum <> '' AND nlic IS NOT NULL"
            $cmd = $conn.CreateCommand()
            $cmd.CommandText = $sql
            $cmd.CommandTimeout = $QueryTimeoutSec
            [void]$cmd.Parameters.AddWithValue('@win', $negDays)
            $r = $cmd.ExecuteReader()
            $count = 0
            while ($r.Read()) {
                $count++
                # MAWB лежит в reg_id (например "425-10390671_" или
                # "298-52164173," — с trailing punctuation от Декларанта).
                $rawReg = "$($r['reg_id'])"
                $mawb = ''
                if ($rawReg -match '(\d{3}-\d{8})') {
                    $mawb = $matches[1]
                }
                $obj = [PSCustomObject]@{
                    document_id = "$($r['DocumentID'])"
                    main_id = "$($r['main_id'])"
                    in_date = if ($r['InDate'] -ne [DBNull]::Value) { ([datetime]$r['InDate']).ToString('yyyy-MM-ddTHH:mm:ss') } else { '' }
                    do1_date_present = if ($r['DO1DatePresent'] -ne [DBNull]::Value) { ([datetime]$r['DO1DatePresent']).ToString('yyyy-MM-ddTHH:mm:ss') } else { '' }
                    nreg_tamnum = "$($r['nreg_tamnum'])"
                    td = "$($r['td'])"
                    nlic = "$($r['nlic'])"
                    ncar = "$($r['ncar'])"
                    reg_id = $rawReg
                    mawb = $mawb
                }
                $obj | ConvertTo-Json -Compress
            }
            $r.Close()
            [Console]::Error.WriteLine("svh_query list-do1: $count rows")
        }
        default {
            Write-Error "Unknown --Op: $Op (expected list|fetch|list-do1)"
            exit 4
        }
    }
} finally {
    $conn.Close()
}
