# 補充的中繼憑證

| 檔案 | 用途 | 來源 | SHA-256 指紋 | 有效期 |
|---|---|---|---|---|
| `twca_secure_ssl_ca_2023g3.pem` | 消防署法令查詢系統（law.nfa.gov.tw）的 TLS 驗證 | 該站憑證 Authority Information Access 所列官方位址 `http://sslserver.twca.com.tw/cacert/secure_sha2_2023G3.crt`（2026-10-01 下載） | `1A:2C:75:FD:09:6E:04:99:E9:FF:6A:C7:4E:52:6F:61:EA:AE:3E:DF:C8:C2:EA:44:36:FE:E0:C2:4D:8B:7D:0E` | 2023-10-16 ～ 2030-10-16 |

原因：law.nfa.gov.tw 的憑證由「TWCA Secure SSL Certification Authority」簽發，但伺服器附上的是不相干的中華電信 HiPKI 中繼憑證，
一般 TLS 用戶端（Python、Linux OpenSSL）因此報「unable to verify the first certificate」。
補上正確的中繼憑證後，驗證仍照常鏈到受信任的「TWCA Global Root CA」，不需要關閉任何驗證。2030 年到期前要更新。
