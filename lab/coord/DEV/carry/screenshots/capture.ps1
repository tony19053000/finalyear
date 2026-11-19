param($dir)
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$b=[System.Windows.Forms.SystemInformation]::VirtualScreen
$end=(Get-Date).AddMinutes(15); $i=0; $last=$null
while((Get-Date) -lt $end){
  $bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height
  $g=[System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size); $g.Dispose()
  $ms=New-Object IO.MemoryStream; $bmp.Save($ms,[System.Drawing.Imaging.ImageFormat]::Png)
  $h=[BitConverter]::ToString([Security.Cryptography.SHA1]::Create().ComputeHash($ms.ToArray()))
  if($h -ne $last){$i++; [IO.File]::WriteAllBytes((Join-Path $dir ("raw-{0:D3}.png" -f $i)),$ms.ToArray()); $last=$h}
  $bmp.Dispose(); Start-Sleep -Seconds 2
}
