# 내 Windows PC로 인터넷에 공개하기 (Cloudflare Tunnel)

사용자 200명 정도를 이 PC 한 대로 받는 방법입니다. 공유기 포트를 열거나 고정 IP를 받을 필요가 없고,
HTTPS 인증서도 Cloudflare가 처리합니다. 비용은 도메인(연 2만원 안팎)뿐입니다.

```
사용자 ──HTTPS──> Cloudflare ──터널──> cloudflared (이 PC) ──> sequenceguider serve (127.0.0.1:8765)
```

## 0. 준비

- 도메인 하나 (예: `tango.example.com`). Cloudflare에 사이트로 등록해 두세요(무료 요금제).
- 이 PC에 SequenceGuider가 설치되어 있고 `uv run sequenceguider serve`가 동작하는 상태.
- 레슨 폴더(`--home`)는 넉넉한 디스크에. 아래 예시는 `D:\tango`.

## 1. 터널 만들기 (cloudflared)

1. PowerShell(관리자)에서 설치: `winget install --id Cloudflare.cloudflared`
2. Cloudflare 대시보드의 Zero Trust > Networks > Tunnels에서 터널을 만듭니다(이름 예: `sequenceguider`).
   커넥터 설치 화면에 나오는 `cloudflared service install <토큰>` 명령을 관리자 PowerShell에 붙여 넣으면
   Windows 서비스로 등록되어 PC가 켜질 때마다 자동으로 연결됩니다.
3. 같은 터널의 Public Hostname에 `tango.example.com` → `http://localhost:8765` 를 추가합니다.

토큰은 비밀번호처럼 다루세요. 저장소나 채팅에 올리지 마세요.

## 2. 서버 실행 설정 확인

[`deploy/windows/start-server.cmd`](../deploy/windows/start-server.cmd)의 위쪽 네 줄(저장소 경로, `--home` 폴더,
공개 주소, 포트)을 고칩니다. 이 스크립트는 다음 옵션으로 서버를 띄웁니다.

| 옵션 | 뜻 |
| --- | --- |
| `--host 127.0.0.1` | 이 PC 안에서만 받습니다. 바깥에서는 터널로만 들어옵니다 |
| `--behind-proxy` | 방문자의 실제 주소(`CF-Connecting-IP`)와 https 여부를 터널 헤더에서 읽습니다. 로그인 실패 제한이 방문자별로 동작하고, 쿠키에 `Secure`가 붙습니다 |
| `--public-url https://…` | 사람들이 쓰는 주소. 쿠키를 항상 `Secure`로, 브라우저에 https만 쓰라고(HSTS) 알립니다 |
| `--signup invite` | 초대 코드가 있어야 가입. 코드는 웹의 기록 > 계정, 또는 `sequenceguider users invite -n 10` |
| `--monthly-limit 4` | 회원 한 명이 한 달에 시작할 수 있는 분석 수. 분석마다 Claude API 비용(2시간 레슨 약 1달러)과 GPU 시간이 들어요 |
| `--max-jobs 1` | 분석은 한 번에 하나만. 나머지는 차례를 기다리고, 서버를 다시 켜도 대기 중인 건 이어서 처리됩니다 |

먼저 한 번 직접 실행해 보고 `https://tango.example.com` 에 접속되는지 확인하세요.

## 3. PC가 켜지면 서버도 자동 시작

관리자 PowerShell에서 (경로와 계정은 바꿔서):

```powershell
schtasks /Create /TN "SequenceGuider" /SC ONSTART /DELAY 0001:00 /RL LIMITED `
  /RU "$env:USERDOMAIN\$env:USERNAME" /RP * `
  /TR "C:\Users\YOU\projects\SequenceGuider\deploy\windows\start-server.cmd"
```

`/RP *`는 Windows 비밀번호를 물어봅니다. 그래야 로그인하지 않은 상태에서도 서버가 뜹니다.
작업 스케줄러에서 이 작업의 "설정" 탭의 "작업이 실패하면 다시 시작" 을 켜 두면 더 안전합니다.

## 4. 매일 백업

[`deploy/windows/backup.cmd`](../deploy/windows/backup.cmd)의 경로를 고친 뒤:

```powershell
schtasks /Create /TN "SequenceGuider backup" /SC DAILY /ST 04:00 /RU "$env:USERDOMAIN\$env:USERNAME" /RP * `
  /TR "C:\Users\YOU\projects\SequenceGuider\deploy\windows\backup.cmd"
```

`records.db`(계정과 연습 기록)만 백업합니다. 서버가 켜져 있어도 안전한 방식입니다. 레슨 폴더(영상, 분석 결과)는
용량이 크니 외장 디스크나 클라우드 동기화로 따로 챙기세요. 한 달에 한 번은 백업 파일로 서버를 띄워 보세요
(`serve --home <임시 폴더>`에 백업 파일을 `records.db`로 복사).

## 5. 꺼지지 않게

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

Windows 업데이트의 "사용 시간"을 사람들이 많이 쓰는 시간대로 맞추면 그 시간에는 재부팅하지 않습니다.
재부팅되더라도 3단계 덕분에 서버와 터널은 저절로 다시 올라옵니다.

## 6. 꺼졌을 때 알림

UptimeRobot 같은 무료 감시 서비스에 `https://tango.example.com/healthz` 를 5분 간격으로 등록하세요.
로그인 없이 `{"ok": true}`만 돌려주는 주소라 노출되어도 괜찮습니다.

## 알아 둘 점

- **영상 전송**: 지금은 영상을 이 PC가 직접 보냅니다. 집 업로드 속도가 동시에 볼 수 있는 사람 수를 정하고,
  Cloudflare 무료 요금제는 터널로 영상을 대량으로 보내는 걸 약관에서 제한할 수 있습니다. 동시 시청이 늘기 전에
  영상을 객체 저장소(R2)로 옮기는 2단계를 진행하세요.
- **디스크**: 회원이 올린 영상은 `<home>/users/<번호>/` 에 쌓입니다. 2시간 영상 하나가 1~2GB이니 여유 공간을 자주 확인하세요. 남은 공간이 5GB 아래로 내려가면 업로드를 받지 않습니다.
- **관리자 기능**: 관리자는 "이 PC의 파일 경로"로 영상을 추가할 수도 있습니다. 관리자 계정 비밀번호는 길게 쓰고
  다른 사람에게 관리자 권한을 주지 마세요.
- **탈퇴**: 회원은 기록 > 계정에서 비밀번호를 넣고 직접 탈퇴할 수 있고, 계정과 기록이 바로 지워집니다.
  관리자는 `sequenceguider users delete <아이디>`로 지울 수 있습니다.
