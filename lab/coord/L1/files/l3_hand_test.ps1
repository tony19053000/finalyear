# L3 by-hand CPU test - run in PowerShell, send the printed lines back to L1
$py = (Get-ChildItem C:\ProgramData\MeshForge\envs\venv\*\Scripts\python.exe | Sort-Object LastWriteTime | Select-Object -Last 1).FullName
& $py -c "import time;t=time.perf_counter();n=3_000_000;s=bytearray([1])*(n+1);s[0]=s[1]=0;[s.__setitem__(slice(i*i,n+1,i),bytes(len(range(i*i,n+1,i)))) for i in range(2,int(n**.5)+1) if s[i]];print('primes:',sum(s),f'{time.perf_counter()-t:.2f} s')"
& $py -c "import numpy as np,time;a=np.random.rand(1500,1500);t=time.perf_counter();[a@a for _ in range(20)];print(f'matmul x20: {time.perf_counter()-t:.1f} s')"
