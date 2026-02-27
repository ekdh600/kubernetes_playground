"""
key_manager.py — 플레이그라운드별 임시 SSH 키 쌍 생성

역할:
- 플레이그라운드 생성 시마다 새로운 RSA 4096-bit 키 쌍을 동적으로 생성한다.
- 정적 자격증명 없이 각 세션에 고유한 SSH 접속 키를 제공한다.

[보안 설계]
- 세션마다 새 키를 생성하므로 키 유출 시 피해 범위가 해당 플레이그라운드로 한정된다.
- RSA 4096-bit: 2048-bit 대비 보안 강도가 높고 현재 K8s 환경의 성능으로 감당 가능하다.
- NoEncryption: 개인키에 패스프레이즈를 설정하지 않는다.
  이유: 브라우저 터미널(xterm.js)이 패스프레이즈 입력을 지원하지 않고,
  개인키는 K8s Secret(서버측)에만 저장되어 클라이언트에 노출되지 않는다.
"""

from cryptography.hazmat.primitives import serialization as crypto_serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend


def generate_ssh_key_pair() -> tuple[str, str]:
    """
    OpenSSH 호환 RSA 키 쌍을 생성하여 반환한다.

    Returns:
        private_key (str): PEM 형식 OpenSSH 개인키 문자열.
            "-----BEGIN OPENSSH PRIVATE KEY-----" 형식으로 시작한다.
            ssh -i 옵션, MobaXterm, PuTTYgen import 등 모든 현대 SSH 클라이언트와 호환된다.

        public_key (str): OpenSSH authorized_keys 형식 공개키.
            "ssh-rsa AAAA..." 형식이며 파드의 /keys/authorized_keys에 복사된다.
    """
    key = rsa.generate_private_key(
        backend=default_backend(),
        public_exponent=65537,  # 표준 RSA 공개 지수. 65537은 소수이면서 계산 효율이 높다.
        key_size=4096,          # 4096-bit: NIST 권장 최소값(2048)보다 강화된 보안 수준
    )

    # OpenSSH 포맷으로 직렬화.
    # PKCS#8이나 PKCS#1 포맷 대신 OpenSSH 포맷을 사용하는 이유:
    # ssh 클라이언트가 직접 -i 옵션으로 사용할 수 있는 표준 포맷이기 때문이다.
    private_key = key.private_bytes(
        encoding=crypto_serialization.Encoding.PEM,
        format=crypto_serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=crypto_serialization.NoEncryption(),  # 패스프레이즈 없음
    ).decode("utf-8")

    # authorized_keys 파일에 직접 쓸 수 있는 공개키 형식으로 직렬화
    public_key = (
        key.public_key()
        .public_bytes(
            encoding=crypto_serialization.Encoding.OpenSSH,
            format=crypto_serialization.PublicFormat.OpenSSH,
        )
        .decode("utf-8")
    )

    return private_key, public_key
