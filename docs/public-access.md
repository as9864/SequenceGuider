# 밖에서 접속하기 (Cloudflare Tunnel)

이 PC에서 돌아가는 `sequenceguider serve`를 인터넷 어디서나 `https://<내 주소>`로 열 수 있게 합니다.
공유기 포트포워딩은 하지 않습니다. PC의 `cloudflared`가 Cloudflare로 **나가는** 연결만 만들고,
방문자는 Cloudflare(HTTPS)를 거쳐 그 연결로만 들어옵니다. 그래서 집 IP가 드러나지 않고, 열린 포트도 없습니다.

```
휴대폰/친구 브라우저 ──HTTPS──▶ Cloudflare (Access 로그인, 요청 제한) ──터널──▶ 이 PC의 cloudflared ──▶ 127.0.0.1:8765 (serve --public)
```

## 준비물

- Cloudflare 계정 (무료)
- Cloudflare에 연결된 도메인 하나 (예: `example.com`, 1년 1~2만 원 정도. Cloudflare에서 바로 살 수도 있어요)
  - 도메인 없이 시험만 해 보려면 맨 아래 "임시 주소로 시험하기"

## 1. 관리자 계정 먼저 만들기

`--public`은 계정이 하나도 없으면 시작하지 않습니다 (안 그러면 주소를 처음 연 낯선 사람이 관리자가 됩니다).

```powershell
uv run sequenceguider users list --home data
uv run sequenceguider users add <아이디> --home data   # 없을 때만. 첫 계정이 관리자. 비밀번호는 길게 (12자 이상 권장)
```

## 2. 서버를 공개 모드로 켜기

```powershell
uv run sequenceguider serve --home data --public --no-open
```

`--public`이 하는 일:

- 127.0.0.1에서만 받습니다 (`--host 0.0.0.0`과 같이 쓸 수 없어요). 밖에서는 터널로만 들어옵니다
- 로그인 쿠키에 `Secure`를 붙여 HTTPS로만 오가게 하고, HSTS·nosniff·프레임 차단 헤더를 보냅니다
- 새 가입을 기본으로 막습니다. 다른 사람 계정은 `users add`로 만들어 주세요 (가입을 열려면 `--signup`)
- 비밀번호 틀림 제한(15분에 10번)을 방문자의 실제 IP(Cloudflare가 알려 주는 `CF-Connecting-IP`) 기준으로 셉니다
- 밖에서 들어온 요청은 영상 추가·분석 때 **링크(유튜브 등)만** 넣을 수 있어요. 이 PC의 파일 경로는 PC에서 직접 열었을 때만 됩니다
  (관리자 비밀번호가 새도 PC의 아무 파일이나 읽히지 않게)

서버는 디버그 모드가 없고 오류 내용(스택)을 화면에 보내지 않습니다. 계정 DB(`data/records.db`)는 웹 주소로 열 수 없습니다.

## 3. 터널 만들기 (Cloudflare 대시보드에서, 한 번만)

1. <https://one.dash.cloudflare.com> → **Networks → Tunnels → Create a tunnel** → **Cloudflared** 선택, 이름 `sequenceguider`
2. 운영체제 **Windows**를 고르면 `cloudflared.exe service install <긴 토큰>` 명령이 나옵니다.
   - cloudflared가 없으면 먼저: `winget install --id Cloudflare.cloudflared`
   - 그 명령을 **관리자 권한 PowerShell**에 붙여 넣어 실행합니다. PC를 켜면 터널이 자동으로 다시 연결돼요
   - 토큰은 비밀번호와 같습니다. 채팅이나 저장소에 붙여 넣지 마세요
3. **Public hostname** 추가: Subdomain `sg`, Domain `example.com`, Service `HTTP` · `127.0.0.1:8765`
4. 저장하면 `https://sg.example.com`이 열립니다 (2번 서버가 켜져 있어야 해요)

## 4. 문 앞에 잠금 하나 더 (권장)

### Cloudflare Access (아는 사람만, 50명까지 무료)

**Zero Trust → Access → Applications → Add → Self-hosted**, 도메인 `sg.example.com`,
정책 **Allow** · Include **Emails**에 들어올 사람 이메일을 적습니다.
처음 열면 Cloudflare가 이메일로 6자리 코드를 보내고, 통과한 사람만 SequenceGuider 로그인 화면을 봅니다.
낯선 사람은 앱에 요청 하나도 보낼 수 없어요.

사용자가 50명을 넘으면 Access 대신 아래 요청 제한 + 앱 로그인으로 갑니다.

### 로그인 요청 제한 (무료 플랜 규칙 1개)

**대시보드 → 도메인 → Security → WAF → Rate limiting rules → Create**:
URI Path equals `/api/auth/login`, 같은 IP에서 10초에 5번 넘으면 10초 차단.

### 그 밖에

- **SSL/TLS → Edge Certificates → Always Use HTTPS** 켜기
- **Security → Bots → Bot Fight Mode** 켜기

## 계속 켜 두기

- PC가 절전으로 들어가면 주소가 안 열립니다. **설정 → 시스템 → 전원**에서 전원 연결 시 절전 "안 함"
- 서버(2번)는 창을 닫으면 꺼집니다. 로그인할 때 자동으로 켜려면 작업 스케줄러에
  `uv run sequenceguider serve --home data --public --no-open`(시작 위치: 이 저장소 폴더)을 "로그온할 때"로 등록하세요

## 끄기

- 잠깐: 서버 창에서 Ctrl+C (주소는 502 오류가 나요)
- 완전히: 관리자 PowerShell에서 `cloudflared service uninstall`, 대시보드에서 터널 삭제

## 임시 주소로 시험하기 (도메인 없이)

```powershell
cloudflared tunnel --url http://127.0.0.1:8765
```

`https://<무작위>.trycloudflare.com` 주소가 나옵니다. 끄면 사라지고 켤 때마다 바뀌며 Access를 붙일 수 없으니 시험용으로만 쓰세요.
