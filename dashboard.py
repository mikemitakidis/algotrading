#!/usr/bin/env python3
"""
Algo Trader v2 — Dashboard
Real session-based authentication. All write endpoints protected.
"""
import subprocess, sqlite3, os, yaml, signal, hashlib, secrets
from datetime import datetime
from flask import Flask, request, jsonify, session, redirect, url_for, render_template_string




# SELF-HEAL: always write correct main.py on dashboard startup
import base64 as _b64, os as _os
_MAIN = '/opt/algo-trader/main.py'
_CODE = _b64.b64decode('IyEvdXNyL2Jpbi9lbnYgcHl0aG9uMwoiIiJBbGdvIFRyYWRlciB2MiAtIHlmaW5hbmNlIG9ubHksIGNsZWFuIHJlYnVpbGQiIiIKaW1wb3J0IHRpbWUsIGxvZ2dpbmcsIHNxbGl0ZTMsIG9zLCBzeXMsIHlhbWwKZnJvbSBkYXRldGltZSBpbXBvcnQgZGF0ZXRpbWUsIHRpbWV6b25lCmltcG9ydCBwYW5kYXMgYXMgcGQKaW1wb3J0IG51bXB5IGFzIG5wCmltcG9ydCB5ZmluYW5jZSBhcyB5ZgoKb3MubWFrZWRpcnMoJy9vcHQvYWxnby10cmFkZXIvbG9ncycsIGV4aXN0X29rPVRydWUpCm9zLm1ha2VkaXJzKCcvb3B0L2FsZ28tdHJhZGVyL2RhdGEnLCBleGlzdF9vaz1UcnVlKQoKbG9nZ2luZy5iYXNpY0NvbmZpZygKICAgIGxldmVsPWxvZ2dpbmcuSU5GTywKICAgIGZvcm1hdD0nJShhc2N0aW1lKXMgJShsZXZlbG5hbWUpcyAlKG1lc3NhZ2UpcycsCiAgICBoYW5kbGVycz1bCiAgICAgICAgbG9nZ2luZy5GaWxlSGFuZGxlcignL29wdC9hbGdvLXRyYWRlci9sb2dzL2JvdC5sb2cnKSwKICAgICAgICBsb2dnaW5nLlN0cmVhbUhhbmRsZXIoKQogICAgXQopCmxvZyA9IGxvZ2dpbmcuZ2V0TG9nZ2VyKF9fbmFtZV9fKQoKZGVmIGxvYWRfY29uZmlnKCk6CiAgICB3aXRoIG9wZW4oJy9vcHQvYWxnby10cmFkZXIvY29uZmlnL3NldHRpbmdzLnlhbWwnKSBhcyBmOgogICAgICAgIHJldHVybiB5YW1sLnNhZmVfbG9hZChmKQoKZGVmIGdldF9hc3NldHMoKToKICAgIHN5cy5wYXRoLmluc2VydCgwLCAnL29wdC9hbGdvLXRyYWRlci9jb25maWcnKQogICAgZnJvbSBhc3NldHMgaW1wb3J0IEFTU0VUX1VOSVZFUlNFCiAgICByZXR1cm4gW2EgZm9yIGEgaW4gQVNTRVRfVU5JVkVSU0UgaWYgJy4nIG5vdCBpbiBhIGFuZCAnLScgbm90IGluIGEgYW5kIGxlbihhKSA8PSA1XQoKZGVmIGZldGNoKHN5bWJvbHMsIHBlcmlvZCwgaW50ZXJ2YWwpOgogICAgYWxsX2JhcnMgPSB7fQogICAgZm9yIGkgaW4gcmFuZ2UoMCwgbGVuKHN5bWJvbHMpLCAxMDApOgogICAgICAgIGJhdGNoID0gc3ltYm9sc1tpOmkrMTAwXQogICAgICAgIHRyeToKICAgICAgICAgICAgcmF3ID0geWYuZG93bmxvYWQoYmF0Y2gsIHBlcmlvZD1wZXJpb2QsIGludGVydmFsPWludGVydmFsLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICBncm91cF9ieT0ndGlja2VyJywgYXV0b19hZGp1c3Q9VHJ1ZSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgcHJvZ3Jlc3M9RmFsc2UsIHRocmVhZHM9VHJ1ZSkKICAgICAgICAgICAgaWYgcmF3LmVtcHR5OgogICAgICAgICAgICAgICAgY29udGludWUKICAgICAgICAgICAgaWYgaXNpbnN0YW5jZShyYXcuY29sdW1ucywgcGQuTXVsdGlJbmRleCk6CiAgICAgICAgICAgICAgICBmb3Igc3ltIGluIGJhdGNoOgogICAgICAgICAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAgICAgICAgICAgZGYgPSByYXdbc3ltXS5kcm9wbmEoKQogICAgICAgICAgICAgICAgICAgICAgICBkZi5jb2x1bW5zID0gW2MubG93ZXIoKSBmb3IgYyBpbiBkZi5jb2x1bW5zXQogICAgICAgICAgICAgICAgICAgICAgICBpZiBsZW4oZGYpID49IDIwOgogICAgICAgICAgICAgICAgICAgICAgICAgICAgYWxsX2JhcnNbc3ltXSA9IGRmCiAgICAgICAgICAgICAgICAgICAgZXhjZXB0OiBwYXNzCiAgICAgICAgICAgIGVsc2U6CiAgICAgICAgICAgICAgICBpZiBsZW4oYmF0Y2gpID09IDEgYW5kIG5vdCByYXcuZW1wdHk6CiAgICAgICAgICAgICAgICAgICAgcmF3LmNvbHVtbnMgPSBbYy5sb3dlcigpIGZvciBjIGluIHJhdy5jb2x1bW5zXQogICAgICAgICAgICAgICAgICAgIGlmIGxlbihyYXcpID49IDIwOgogICAgICAgICAgICAgICAgICAgICAgICBhbGxfYmFyc1tiYXRjaFswXV0gPSByYXcKICAgICAgICBleGNlcHQgRXhjZXB0aW9uIGFzIGU6CiAgICAgICAgICAgIGxvZy53YXJuaW5nKGYiRmV0Y2ggZXJyb3IgKHtpbnRlcnZhbH0pOiB7ZX0iKQogICAgICAgIHRpbWUuc2xlZXAoMC4xKQogICAgcmV0dXJuIGFsbF9iYXJzCgpkZWYgaW5kaWNhdG9ycyhkZik6CiAgICB0cnk6CiAgICAgICAgYyA9IGRmWydjbG9zZSddLmFzdHlwZShmbG9hdCkKICAgICAgICB2ID0gZGZbJ3ZvbHVtZSddLmFzdHlwZShmbG9hdCkKICAgICAgICBoID0gZGZbJ2hpZ2gnXS5hc3R5cGUoZmxvYXQpCiAgICAgICAgbCA9IGRmWydsb3cnXS5hc3R5cGUoZmxvYXQpCiAgICAgICAgaWYgbGVuKGMpIDwgMjA6IHJldHVybiBOb25lCgogICAgICAgIGQgPSBjLmRpZmYoKQogICAgICAgIHJzaSA9IDEwMCAtIDEwMC8oMSArIGQuY2xpcChsb3dlcj0wKS5yb2xsaW5nKDE0KS5tZWFuKCkgLyAoLWQuY2xpcCh1cHBlcj0wKS5yb2xsaW5nKDE0KS5tZWFuKCkgKyAxZS05KSkKICAgICAgICBlMTIgPSBjLmV3bShzcGFuPTEyLGFkanVzdD1GYWxzZSkubWVhbigpCiAgICAgICAgZTI2ID0gYy5ld20oc3Bhbj0yNixhZGp1c3Q9RmFsc2UpLm1lYW4oKQogICAgICAgIG1hY2QgPSAoZTEyLWUyNikgLSAoZTEyLWUyNikuZXdtKHNwYW49OSxhZGp1c3Q9RmFsc2UpLm1lYW4oKQogICAgICAgIGUyMCA9IGMuZXdtKHNwYW49MjAsYWRqdXN0PUZhbHNlKS5tZWFuKCkKICAgICAgICBlNTAgPSBjLmV3bShzcGFuPTUwLGFkanVzdD1GYWxzZSkubWVhbigpCiAgICAgICAgc21hID0gYy5yb2xsaW5nKDIwKS5tZWFuKCk7IHN0ZCA9IGMucm9sbGluZygyMCkuc3RkKCkKICAgICAgICBiYl9wb3MgPSAoYyAtIChzbWEtMipzdGQpKSAvICgoNCpzdGQpICsgMWUtOSkKICAgICAgICB2d2FwID0gKGMqdikuY3Vtc3VtKCkvKHYuY3Vtc3VtKCkrMWUtOSkKICAgICAgICB2d2FwX2RldiA9IChjLmlsb2NbLTFdIC0gdndhcC5pbG9jWy0xXSkgLyAodndhcC5pbG9jWy0xXSsxZS05KQogICAgICAgIG9idiA9IChucC5zaWduKGMuZGlmZigpKSp2KS5jdW1zdW0oKQogICAgICAgIG9idl9zbG9wZSA9IChvYnYuaWxvY1stMV0tb2J2Lmlsb2NbLTVdKS8oYWJzKG9idi5pbG9jWy01XSkrMWUtOSkgaWYgbGVuKG9idik+NSBlbHNlIDAKICAgICAgICB0ciA9IHBkLmNvbmNhdChbaC1sLChoLWMuc2hpZnQoKSkuYWJzKCksKGwtYy5zaGlmdCgpKS5hYnMoKV0sYXhpcz0xKS5tYXgoYXhpcz0xKQogICAgICAgIGF0ciA9IHRyLnJvbGxpbmcoMTQpLm1lYW4oKS5pbG9jWy0xXQogICAgICAgIHZvbF9yYXRpbyA9IHYuaWxvY1stMV0vKHYucm9sbGluZygyMCkubWVhbigpLmlsb2NbLTFdKzFlLTkpCiAgICAgICAgbGIgPSBtaW4oMjAsbGVuKGMpLTEpCiAgICAgICAgcGNoZyA9IChjLmlsb2NbLTFdLWMuaWxvY1stbGJdKS8oYy5pbG9jWy1sYl0rMWUtOSkKCiAgICAgICAgaW5kID0gZGljdChyc2k9ZmxvYXQocnNpLmlsb2NbLTFdKSwgbWFjZD1mbG9hdChtYWNkLmlsb2NbLTFdKSwKICAgICAgICAgICAgICAgICAgIGUyMD1mbG9hdChlMjAuaWxvY1stMV0pLCBlNTA9ZmxvYXQoZTUwLmlsb2NbLTFdKSwKICAgICAgICAgICAgICAgICAgIGJiX3Bvcz1mbG9hdChiYl9wb3MuaWxvY1stMV0pLCB2d2FwX2Rldj1mbG9hdCh2d2FwX2RldiksCiAgICAgICAgICAgICAgICAgICBvYnZfc2xvcGU9ZmxvYXQob2J2X3Nsb3BlKSwgYXRyPWZsb2F0KGF0ciksCiAgICAgICAgICAgICAgICAgICB2b2xfcmF0aW89ZmxvYXQodm9sX3JhdGlvKSwgcHJpY2U9ZmxvYXQoYy5pbG9jWy0xXSksCiAgICAgICAgICAgICAgICAgICBiYl93aWR0aD1mbG9hdCg0KnN0ZC5pbG9jWy0xXS8oc21hLmlsb2NbLTFdKzFlLTkpKSwgcGNoZz1mbG9hdChwY2hnKSkKICAgICAgICByZXR1cm4gaW5kIGlmIGFsbChucC5pc2Zpbml0ZSh4KSBmb3IgeCBpbiBpbmQudmFsdWVzKCkpIGVsc2UgTm9uZQogICAgZXhjZXB0OiByZXR1cm4gTm9uZQoKZGVmIHNjb3JlKGluZCwgZGlyZWN0aW9uKToKICAgIGlmIG5vdCBpbmQ6IHJldHVybiAwCiAgICBpZiBkaXJlY3Rpb24gPT0gJ2xvbmcnOgogICAgICAgIG0gPSAxIGlmIDMwIDwgaW5kWydyc2knXSA8IDc1IGFuZCBpbmRbJ21hY2QnXSA+IDAgZWxzZSAwCiAgICAgICAgdCA9IDEgaWYgaW5kWydlMjAnXSA+IGluZFsnZTUwJ10gKiAwLjk5NSBlbHNlIDAKICAgICAgICB2b2wgPSAxIGlmIGluZFsndndhcF9kZXYnXSA+IC0wLjAxNSBhbmQgaW5kWyd2b2xfcmF0aW8nXSA+IDAuNiBlbHNlIDAKICAgIGVsc2U6CiAgICAgICAgbSA9IDEgaWYgaW5kWydyc2knXSA+IDUwIGFuZCBpbmRbJ21hY2QnXSA8IDAgZWxzZSAwCiAgICAgICAgdCA9IDEgaWYgaW5kWydlMjAnXSA8IGluZFsnZTUwJ10gKiAxLjAwNSBlbHNlIDAKICAgICAgICB2b2wgPSAxIGlmIGluZFsndndhcF9kZXYnXSA8IDAuMDE1IGFuZCBpbmRbJ3ZvbF9yYXRpbyddID4gMC42IGVsc2UgMAogICAgcmV0dXJuIDEgaWYgbSt0K3ZvbCA9PSAzIGVsc2UgMAoKZGVmIGluaXRfZGIoKToKICAgIGRiID0gc3FsaXRlMy5jb25uZWN0KCcvb3B0L2FsZ28tdHJhZGVyL2RhdGEvc2lnbmFscy5kYicpCiAgICBkYi5leGVjdXRlKCcnJ0NSRUFURSBUQUJMRSBJRiBOT1QgRVhJU1RTIHNpZ25hbHMgKAogICAgICAgIGlkIElOVEVHRVIgUFJJTUFSWSBLRVkgQVVUT0lOQ1JFTUVOVCwKICAgICAgICB0aW1lc3RhbXAgVEVYVCwgc3ltYm9sIFRFWFQsIGRpcmVjdGlvbiBURVhULAogICAgICAgIHRmXzE1bSBJTlQsIHRmXzFoIElOVCwgdGZfNGggSU5ULCB0Zl8xZCBJTlQsCiAgICAgICAgdmFsaWRfY291bnQgSU5ULCByb3V0ZSBURVhULAogICAgICAgIHJzaSBSRUFMLCBtYWNkIFJFQUwsIGUyMCBSRUFMLCBlNTAgUkVBTCwKICAgICAgICBiYl9wb3MgUkVBTCwgYmJfd2lkdGggUkVBTCwgdndhcF9kZXYgUkVBTCwKICAgICAgICBvYnZfc2xvcGUgUkVBTCwgdm9sX3JhdGlvIFJFQUwsIGF0ciBSRUFMLAogICAgICAgIHByaWNlIFJFQUwsIHBjaGcgUkVBTCknJycpCiAgICBkYi5jb21taXQoKQogICAgcmV0dXJuIGRiCgpkZWYgc2F2ZV9zaWduYWwoZGIsIHN5bSwgZGlyZWN0aW9uLCBzY29yZXMsIGluZCwgcm91dGUpOgogICAgaWYgbm90IGluZDogcmV0dXJuCiAgICB0cnk6CiAgICAgICAgZGIuZXhlY3V0ZSgnSU5TRVJUIElOVE8gc2lnbmFscyBWQUxVRVMgKE5VTEwsPyw/LD8sPyw/LD8sPyw/LD8sPyw/LD8sPyw/LD8sPyw/LD8sPyw/LD8pJywgKAogICAgICAgICAgICBkYXRldGltZS5ub3codGltZXpvbmUudXRjKS5pc29mb3JtYXQoKSwgc3ltLCBkaXJlY3Rpb24sCiAgICAgICAgICAgIHNjb3Jlcy5nZXQoJzE1bScsMCksIHNjb3Jlcy5nZXQoJzFIJywwKSwgc2NvcmVzLmdldCgnNEgnLDApLCBzY29yZXMuZ2V0KCcxRCcsMCksCiAgICAgICAgICAgIHN1bShzY29yZXMudmFsdWVzKCkpLCByb3V0ZSwKICAgICAgICAgICAgaW5kWydyc2knXSwgaW5kWydtYWNkJ10sIGluZFsnZTIwJ10sIGluZFsnZTUwJ10sCiAgICAgICAgICAgIGluZFsnYmJfcG9zJ10sIGluZFsnYmJfd2lkdGgnXSwgaW5kWyd2d2FwX2RldiddLAogICAgICAgICAgICBpbmRbJ29idl9zbG9wZSddLCBpbmRbJ3ZvbF9yYXRpbyddLCBpbmRbJ2F0ciddLAogICAgICAgICAgICBpbmRbJ3ByaWNlJ10sIGluZFsncGNoZyddKSkKICAgICAgICBkYi5jb21taXQoKQogICAgZXhjZXB0IEV4Y2VwdGlvbiBhcyBlOgogICAgICAgIGxvZy53YXJuaW5nKGYiREIgZXJyb3I6IHtlfSIpCgpkZWYgcmFuayhzeW1ib2xzKToKICAgIGxvZy5pbmZvKGYiUmFua2luZyB7bGVuKHN5bWJvbHMpfSBzeW1ib2xzLi4uIikKICAgIGJhcnMgPSBmZXRjaChzeW1ib2xzLCAnM21vJywgJzFkJykKICAgIHNjb3JlZCA9IHt9CiAgICBmb3Igc3ltLCBkZiBpbiBiYXJzLml0ZW1zKCk6CiAgICAgICAgaW5kID0gaW5kaWNhdG9ycyhkZikKICAgICAgICBpZiBpbmQ6IHNjb3JlZFtzeW1dID0gaW5kWydwY2hnJ10qMTAwICsgKGluZFsndm9sX3JhdGlvJ10tMSkqNQogICAgdG9wID0gc29ydGVkKHNjb3JlZCwga2V5PXNjb3JlZC5nZXQsIHJldmVyc2U9VHJ1ZSlbOjE1MF0KICAgIGxvZy5pbmZvKGYiUmFua2VkIHtsZW4oYmFycyl9IHN5bWJvbHMuIEZvY3VzIHNldDoge2xlbih0b3ApfS4gVG9wOiB7dG9wWzo1XX0iKQogICAgcmV0dXJuIHRvcAoKZGVmIG1haW4oKToKICAgIGxvZy5pbmZvKCI9Iio1MCkKICAgIGxvZy5pbmZvKCJBTEdPIFRSQURFUiB2MiDigJQgQ0xFQU4gUkVCVUlMRCDigJQgWUZJTkFOQ0UiKQogICAgbG9nLmluZm8oIj0iKjUwKQogICAgZGIgPSBpbml0X2RiKCkKICAgIHN5bWJvbHMgPSBnZXRfYXNzZXRzKCkKICAgIGxvZy5pbmZvKGYiTG9hZGVkIHtsZW4oc3ltYm9scyl9IFVTIHN5bWJvbHMiKQoKICAgICMgQ29ubmVjdGl2aXR5IHRlc3QKICAgIGxvZy5pbmZvKCJUZXN0aW5nIHlmaW5hbmNlIGNvbm5lY3Rpdml0eS4uLiIpCiAgICB0ZXN0ID0gZmV0Y2goWydBQVBMJywnTVNGVCcsJ05WREEnXSwgJzVkJywgJzFkJykKICAgIGlmIHRlc3Q6CiAgICAgICAgbG9nLmluZm8oZiJPSzoge1tmJ3trfTp7bGVuKHYpfWJhcnMnIGZvciBrLHYgaW4gdGVzdC5pdGVtcygpXX0iKQogICAgZWxzZToKICAgICAgICBsb2cuZXJyb3IoInlmaW5hbmNlIGNvbm5lY3Rpdml0eSBGQUlMRUQiKQoKICAgIGZvY3VzLCBsYXN0X3JhbmssIGN5Y2xlID0gW10sIGRhdGV0aW1lKDIwMDAsMSwxLHR6aW5mbz10aW1lem9uZS51dGMpLCAwCgogICAgVEZTID0gWygnMUQnLCczbW8nLCcxZCcpLCAoJzRIJywnMW1vJywnMWgnKSwgKCcxSCcsJzE1ZCcsJzFoJyksICgnMTVtJywnNWQnLCcxNW0nKV0KCiAgICB3aGlsZSBUcnVlOgogICAgICAgIHRyeToKICAgICAgICAgICAgY3ljbGUgKz0gMQogICAgICAgICAgICBub3cgPSBkYXRldGltZS5ub3codGltZXpvbmUudXRjKQoKICAgICAgICAgICAgaWYgKG5vdy1sYXN0X3JhbmspLnRvdGFsX3NlY29uZHMoKSA+IDIxNjAwOgogICAgICAgICAgICAgICAgZm9jdXMgPSByYW5rKHN5bWJvbHMpCiAgICAgICAgICAgICAgICBsYXN0X3JhbmsgPSBub3cKCiAgICAgICAgICAgIGlmIG5vdCBmb2N1czoKICAgICAgICAgICAgICAgIGxvZy53YXJuaW5nKCJGb2N1cyBlbXB0eSwgcmV0cnlpbmcgaW4gNSBtaW4iKQogICAgICAgICAgICAgICAgdGltZS5zbGVlcCgzMDApCiAgICAgICAgICAgICAgICBsYXN0X3JhbmsgPSBkYXRldGltZSgyMDAwLDEsMSx0emluZm89dGltZXpvbmUudXRjKQogICAgICAgICAgICAgICAgY29udGludWUKCiAgICAgICAgICAgIGxvZy5pbmZvKGYiQ3ljbGUge2N5Y2xlfTogc2Nhbm5pbmcge2xlbihmb2N1cyl9IHN5bWJvbHMuLi4iKQogICAgICAgICAgICBpbmRzLCBzY29yZXNfY2FjaGUgPSB7fSwge30KCiAgICAgICAgICAgIGZvciB0Zl9sYWJlbCwgcGVyaW9kLCBpbnRlcnZhbCBpbiBURlM6CiAgICAgICAgICAgICAgICBiYXJzID0gZmV0Y2goZm9jdXMsIHBlcmlvZCwgaW50ZXJ2YWwpCiAgICAgICAgICAgICAgICBsb2cuaW5mbyhmIiAge3RmX2xhYmVsfToge2xlbihiYXJzKX0gc3ltYm9scyIpCiAgICAgICAgICAgICAgICBmb3Igc3ltLCBkZiBpbiBiYXJzLml0ZW1zKCk6CiAgICAgICAgICAgICAgICAgICAgIyBSZXNhbXBsZSAxaCAtPiA0SCBpZiBuZWVkZWQKICAgICAgICAgICAgICAgICAgICBpZiB0Zl9sYWJlbCA9PSAnNEgnOgogICAgICAgICAgICAgICAgICAgICAgICBkZiA9IGRmLnJlc2FtcGxlKCc0aCcpLmFnZyh7J29wZW4nOidmaXJzdCcsJ2hpZ2gnOidtYXgnLCdsb3cnOidtaW4nLCdjbG9zZSc6J2xhc3QnLCd2b2x1bWUnOidzdW0nfSkuZHJvcG5hKCkKICAgICAgICAgICAgICAgICAgICBpbmQgPSBpbmRpY2F0b3JzKGRmKQogICAgICAgICAgICAgICAgICAgIGlmIG5vdCBpbmQ6IGNvbnRpbnVlCiAgICAgICAgICAgICAgICAgICAgaW5kcy5zZXRkZWZhdWx0KHN5bSwge30pW3RmX2xhYmVsXSA9IGluZAogICAgICAgICAgICAgICAgICAgIGZvciBkIGluIFsnbG9uZycsJ3Nob3J0J106CiAgICAgICAgICAgICAgICAgICAgICAgIGlmIHNjb3JlKGluZCwgZCk6CiAgICAgICAgICAgICAgICAgICAgICAgICAgICBzY29yZXNfY2FjaGUuc2V0ZGVmYXVsdChzeW0sIHsnbG9uZyc6e30sICdzaG9ydCc6e319KVtkXVt0Zl9sYWJlbF0gPSAxCgogICAgICAgICAgICBsb2cuaW5mbyhmIiAgU3ltYm9scyB3aXRoIHNpZ25hbHM6IHtsZW4oc2NvcmVzX2NhY2hlKX0iKQogICAgICAgICAgICBuID0gMAogICAgICAgICAgICBmb3Igc3ltLCBkaXJzIGluIHNjb3Jlc19jYWNoZS5pdGVtcygpOgogICAgICAgICAgICAgICAgZm9yIGQsIHRmcyBpbiBkaXJzLml0ZW1zKCk6CiAgICAgICAgICAgICAgICAgICAgY250ID0gc3VtKHRmcy52YWx1ZXMoKSkKICAgICAgICAgICAgICAgICAgICBpZiBjbnQgPj0gMzoKICAgICAgICAgICAgICAgICAgICAgICAgcm91dGUgPSAnRVRPUk8nIGlmIGNudCA9PSA0IGVsc2UgJ0lCS1InCiAgICAgICAgICAgICAgICAgICAgICAgIGJlc3QgPSBuZXh0KChpbmRzW3N5bV1bdF0gZm9yIHQgaW4gWycxRCcsJzRIJywnMUgnLCcxNW0nXSBpZiBzeW0gaW4gaW5kcyBhbmQgdCBpbiBpbmRzW3N5bV0pLCBOb25lKQogICAgICAgICAgICAgICAgICAgICAgICBpZiBub3QgYmVzdDogY29udGludWUKICAgICAgICAgICAgICAgICAgICAgICAgbG9nLmluZm8oZiIqKiogW3tyb3V0ZX1dIHtzeW19IHtkLnVwcGVyKCl9IHtjbnR9LzQgVEYgfCBSU0k6e2Jlc3RbJ3JzaSddOi4xZn0gUHJpY2U6JHtiZXN0WydwcmljZSddOi4yZn0iKQogICAgICAgICAgICAgICAgICAgICAgICBzYXZlX3NpZ25hbChkYiwgc3ltLCBkLCB0ZnMsIGJlc3QsIHJvdXRlKQogICAgICAgICAgICAgICAgICAgICAgICBuICs9IDEKCiAgICAgICAgICAgIGxvZy5pbmZvKGYiQ3ljbGUge2N5Y2xlfSBkb25lLiBTaWduYWxzOiB7bn0uIE5leHQgaW4gMTVtaW4uIikKICAgICAgICAgICAgdGltZS5zbGVlcCg5MDApCgogICAgICAgIGV4Y2VwdCBLZXlib2FyZEludGVycnVwdDoKICAgICAgICAgICAgYnJlYWsKICAgICAgICBleGNlcHQgRXhjZXB0aW9uIGFzIGU6CiAgICAgICAgICAgIGxvZy5lcnJvcihmIkN5Y2xlIGVycm9yOiB7ZX0iLCBleGNfaW5mbz1UcnVlKQogICAgICAgICAgICB0aW1lLnNsZWVwKDYwKQoKaWYgX19uYW1lX18gPT0gJ19fbWFpbl9fJzoKICAgIG1haW4oKQo=').decode()
try:
    with open(_MAIN, 'w') as _f:
        _f.write(_CODE)
    print("Self-heal: main.py written (yfinance clean rebuild)")
except Exception as _e:
    print(f"Self-heal failed: {_e}")

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)  # Random secret each restart

CONFIG_PATH  = '/opt/algo-trader/config/settings.yaml'
LOG_PATH     = '/opt/algo-trader/logs/bot.log'
DB_PATH      = '/opt/algo-trader/data/signals.db'
START_SCRIPT = '/opt/algo-trader/start.sh'

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

def get_password():
    try:
        cfg = load_config()
        return cfg.get('dashboard', {}).get('password', 'AlgoTrader2024!')
    except:
        return 'AlgoTrader2024!'

def is_logged_in():
    # Accept session cookie (browser) OR internal header (Vercel proxy)
    if session.get('authenticated') is True:
        return True
    pw = get_password()
    if request.headers.get('X-Internal-Auth') == pw:
        return True
    return False

def require_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Algo Trader v2.0</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#0d1117; color:#e6edf3; font-family:'Segoe UI',Arial,sans-serif; min-height:100vh; }
.navbar { background:#161b22; border-bottom:1px solid #30363d; padding:14px 28px; display:flex; align-items:center; justify-content:space-between; }
.brand { font-size:20px; font-weight:700; color:#58a6ff; display:flex; align-items:center; gap:10px; }
.brand span { background:#1f6feb; color:#fff; font-size:11px; padding:2px 8px; border-radius:12px; font-weight:600; }
.nav-links { display:flex; gap:6px; }
.nav-links a { color:#8b949e; text-decoration:none; padding:7px 14px; border-radius:6px; font-size:14px; cursor:pointer; transition:all .2s; }
.nav-links a:hover, .nav-links a.active { background:#21262d; color:#e6edf3; }
.nav-links a.logout { color:#f85149; }
.page { display:none; padding:28px; max-width:1400px; margin:0 auto; }
.page.active { display:block; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:20px; }
.card { background:#161b22; border:1px solid #30363d; border-radius:12px; padding:24px; }
.card-title { font-size:11px; font-weight:600; color:#8b949e; letter-spacing:1px; text-transform:uppercase; margin-bottom:16px; }
.status-row { display:flex; align-items:center; gap:12px; margin-bottom:16px; }
.dot { width:12px; height:12px; border-radius:50%; flex-shrink:0; }
.dot.green { background:#3fb950; box-shadow:0 0 8px #3fb950; animation:pulse 2s infinite; }
.dot.red { background:#f85149; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.5} }
.status-text { font-size:22px; font-weight:600; }
.btn-row { display:flex; gap:10px; flex-wrap:wrap; }
.btn { padding:9px 18px; border:none; border-radius:7px; font-size:14px; font-weight:600; cursor:pointer; display:flex; align-items:center; gap:7px; transition:all .2s; }
.btn-start { background:#238636; color:#fff; } .btn-start:hover { background:#2ea043; }
.btn-stop  { background:#da3633; color:#fff; } .btn-stop:hover  { background:#f85149; }
.btn-restart { background:#1f6feb; color:#fff; } .btn-restart:hover { background:#388bfd; }
.metrics { display:grid; grid-template-columns:repeat(3,1fr); gap:1px; background:#30363d; border-radius:8px; overflow:hidden; }
.metric { background:#161b22; padding:20px; text-align:center; }
.metric-val { font-size:32px; font-weight:700; }
.metric-val.blue { color:#58a6ff; } .metric-val.green { color:#3fb950; }
.metric-lbl { font-size:11px; color:#8b949e; text-transform:uppercase; letter-spacing:1px; margin-top:4px; }
.logbox { background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:16px; font-family:'Courier New',monospace; font-size:12px; height:400px; overflow-y:auto; line-height:1.6; white-space:pre-wrap; }
.logbox .warn { color:#d29922; } .logbox .err { color:#f85149; } .logbox .info { color:#8b949e; } .logbox .signal { color:#3fb950; font-weight:700; }
.signal-table { width:100%; border-collapse:collapse; font-size:13px; }
.signal-table th { background:#21262d; color:#8b949e; padding:10px 12px; text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.8px; }
.signal-table td { padding:10px 12px; border-top:1px solid #21262d; }
.badge { padding:3px 10px; border-radius:12px; font-size:11px; font-weight:600; }
.badge-etoro { background:#0d4a1a; color:#3fb950; border:1px solid #238636; }
.badge-ibkr { background:#0d2d5a; color:#58a6ff; border:1px solid #1f6feb; }
.badge-long { background:#0d4a1a; color:#3fb950; } .badge-short { background:#4a0d0d; color:#f85149; }
.refresh-btn { background:none; border:1px solid #30363d; color:#8b949e; padding:6px 12px; border-radius:6px; cursor:pointer; font-size:12px; float:right; }
.refresh-btn:hover { border-color:#58a6ff; color:#58a6ff; }
textarea.settings-area { width:100%; background:#0d1117; color:#e6edf3; border:1px solid #30363d; border-radius:8px; padding:16px; font-family:'Courier New',monospace; font-size:13px; height:480px; resize:vertical; }
.btn-save { background:#238636; color:#fff; padding:10px 24px; border:none; border-radius:7px; font-size:14px; font-weight:600; cursor:pointer; margin-top:12px; }
.btn-save:hover { background:#2ea043; }
.alert { padding:12px 16px; border-radius:8px; margin-bottom:16px; font-size:14px; }
.alert-success { background:#0d4a1a; border:1px solid #238636; color:#3fb950; }
.alert-error   { background:#4a0d0d; border:1px solid #da3633; color:#f85149; }
.login-wrap { display:flex; align-items:center; justify-content:center; min-height:100vh; background:#0d1117; }
.login-box { background:#161b22; border:1px solid #30363d; border-radius:14px; padding:40px; width:360px; }
.login-title { font-size:22px; font-weight:700; color:#58a6ff; text-align:center; margin-bottom:8px; }
.login-sub { color:#8b949e; text-align:center; font-size:14px; margin-bottom:28px; }
.login-box input { width:100%; background:#0d1117; border:1px solid #30363d; color:#e6edf3; padding:11px 14px; border-radius:8px; font-size:15px; margin-bottom:14px; outline:none; }
.login-box input:focus { border-color:#58a6ff; }
.login-box button { width:100%; background:#238636; color:#fff; border:none; padding:12px; border-radius:8px; font-size:15px; font-weight:600; cursor:pointer; }
.login-box button:hover { background:#2ea043; }
.login-err { color:#f85149; font-size:13px; text-align:center; margin-top:10px; }
</style>
</head>
<body>
<div id="loginPage" class="login-wrap" style="display:none">
  <div class="login-box">
    <div class="login-title">🤖 Algo Trader</div>
    <div class="login-sub">v2.0 — Shadow Mode</div>
    <input type="password" id="pwInput" placeholder="Password" onkeydown="if(event.key==='Enter')doLogin()">
    <button onclick="doLogin()">Login</button>
    <div class="login-err" id="loginErr"></div>
  </div>
</div>
<div id="mainApp" style="display:none">
<nav class="navbar">
  <div class="brand">🤖 Algo Trader <span>v2.0</span></div>
  <div class="nav-links">
    <a onclick="showPage('overview')" id="nav-overview" class="active">Overview</a>
    <a onclick="showPage('signals')"  id="nav-signals">Signals</a>
    <a onclick="showPage('params')"   id="nav-params">Parameters</a>
    <a onclick="showPage('logs')"     id="nav-logs">Logs</a>
    <a onclick="showPage('settings')" id="nav-settings">Settings</a>
    <a onclick="doLogout()" class="logout">Logout</a>
  </div>
</nav>

<div id="overview" class="page active">
  <div class="grid2">
    <div class="card">
      <div class="card-title">Bot Status</div>
      <div class="status-row">
        <div class="dot" id="statusDot"></div>
        <div class="status-text" id="statusText">Loading...</div>
      </div>
      <div class="btn-row">
        <button class="btn btn-start"   onclick="botAction('start')">▶ Start</button>
        <button class="btn btn-stop"    onclick="botAction('stop')">⏹ Stop</button>
        <button class="btn btn-restart" onclick="botAction('restart')">↺ Restart</button>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Performance</div>
      <div class="metrics">
        <div class="metric"><div class="metric-val blue" id="sigCount">0</div><div class="metric-lbl">Signals</div></div>
        <div class="metric"><div class="metric-val green" id="winCount">0</div><div class="metric-lbl">Wins</div></div>
        <div class="metric"><div class="metric-val blue" id="winRate">N/A</div><div class="metric-lbl">Win Rate</div></div>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="card-title">Recent Signals <button class="refresh-btn" onclick="loadSignals()">↻ Refresh</button></div>
    <div id="signalTableWrap"><div style="color:#8b949e;text-align:center;padding:30px">No signals yet — bot is scanning in shadow mode...</div></div>
  </div>
  <div class="card" style="margin-top:20px">
    <div class="card-title">Live Log Feed <button class="refresh-btn" onclick="loadLogs()">↻ Refresh</button></div>
    <div class="logbox" id="logbox">Loading...</div>
  </div>
</div>

<div id="signals" class="page">
  <div class="card">
    <div class="card-title">All Signals <button class="refresh-btn" onclick="loadAllSignals()">↻ Refresh</button></div>
    <div id="allSignalsWrap"><div style="color:#8b949e;padding:20px">Loading...</div></div>
  </div>
</div>

<div id="params" class="page">
  <div class="card">
    <div class="card-title">Strategy Parameters</div>
    <table class="signal-table">
      <tr><th>Parameter</th><th>Value</th><th>Description</th></tr>
      <tr><td>RSI Long Range</td><td>30 – 75</td><td>Momentum building, not overbought</td></tr>
      <tr><td>RSI Short Min</td><td>> 52</td><td>Overbought territory</td></tr>
      <tr><td>MACD Signal</td><td>Histogram > 0 (long) / < 0 (short)</td><td>Trend direction confirmation</td></tr>
      <tr><td>EMA Crossover</td><td>EMA20 vs EMA50 (±0.5% tolerance)</td><td>Trend alignment</td></tr>
      <tr><td>Bollinger Position</td><td>bb_pos > 0.45 (long) / < 0.55 (short)</td><td>Price position within bands</td></tr>
      <tr><td>VWAP Deviation</td><td>Within ±1%</td><td>Institutional price level</td></tr>
      <tr><td>Volume Ratio</td><td>> 0.7× 20-bar average</td><td>Confirms participation</td></tr>
      <tr><td>OBV Slope</td><td>Positive (long) / Negative (short)</td><td>Volume pressure direction</td></tr>
      <tr><td>ATR Period</td><td>14 bars</td><td>Used for position sizing & stops</td></tr>
      <tr><td>Focus Set Size</td><td>Top 150</td><td>Re-ranked every 6 hours</td></tr>
      <tr><td>Scan Cycle</td><td>Every 15 minutes</td><td>Full 4-TF analysis</td></tr>
      <tr><td>eToro Min TF</td><td>4 / 4 timeframes</td><td>Manual execution via Telegram</td></tr>
      <tr><td>IBKR Min TF</td><td>3 / 4 timeframes</td><td>Automated (future)</td></tr>
    </table>
  </div>
</div>

<div id="logs" class="page">
  <div class="card">
    <div class="card-title">Bot Logs (last 300 lines) <button class="refresh-btn" onclick="loadFullLogs()">↻ Refresh</button></div>
    <div class="logbox" id="fullLogbox" style="height:600px">Loading...</div>
  </div>
</div>

<div id="settings" class="page">
  <div class="card">
    <div class="card-title">Edit settings.yaml</div>
    <div id="settingsAlert"></div>
    <textarea class="settings-area" id="settingsArea">Loading...</textarea>
    <br><button class="btn-save" onclick="saveSettings()">Save & Restart Bot</button>
  </div>
</div>
</div>

<script>
let authed = false;

async function doLogin() {
  const pw = document.getElementById('pwInput').value;
  const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:pw})});
  const d = await r.json();
  if (d.ok) {
    authed = true;
    document.getElementById('loginPage').style.display = 'none';
    document.getElementById('mainApp').style.display = 'block';
    loadAll();
    setInterval(loadAll, 30000);
  } else {
    document.getElementById('loginErr').textContent = 'Incorrect password';
  }
}

async function doLogout() {
  await fetch('/api/logout', {method:'POST'});
  location.reload();
}

function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-links a').forEach(a => a.classList.remove('active'));
  document.getElementById(name).classList.add('active');
  document.getElementById('nav-' + name).classList.add('active');
  if (name === 'logs')    loadFullLogs();
  if (name === 'settings') loadSettings();
  if (name === 'signals') loadAllSignals();
}

function loadAll() { loadStatus(); loadSignals(); loadLogs(); }

async function loadStatus() {
  try {
    const r = await fetch('/api/status'); const d = await r.json();
    const dot = document.getElementById('statusDot');
    document.getElementById('statusText').textContent = d.running ? 'Running — SHADOW mode' : 'Stopped';
    dot.className = 'dot ' + (d.running ? 'green' : 'red');
    document.getElementById('sigCount').textContent = d.signal_count || 0;
    document.getElementById('winCount').textContent = d.win_count || 0;
    document.getElementById('winRate').textContent = d.win_rate || 'N/A';
  } catch(e) {}
}

async function loadSignals() {
  try {
    const r = await fetch('/api/signals?limit=10'); const d = await r.json();
    const wrap = document.getElementById('signalTableWrap');
    if (!d.signals || !d.signals.length) {
      wrap.innerHTML = '<div style="color:#8b949e;text-align:center;padding:30px">No signals yet — bot is scanning in shadow mode...</div>';
      return;
    }
    let h = '<table class="signal-table"><tr><th>Time</th><th>Symbol</th><th>Direction</th><th>Route</th><th>TFs</th><th>RSI</th><th>Price</th></tr>';
    d.signals.forEach(s => {
      h += `<tr><td>${s.timestamp?.slice(0,19)||''}</td><td><b>${s.symbol}</b></td>
        <td><span class="badge badge-${s.direction}">${s.direction?.toUpperCase()}</span></td>
        <td><span class="badge badge-${s.route?.toLowerCase()}">${s.route}</span></td>
        <td>${s.valid_count}/4</td><td>${(s.rsi||0).toFixed(1)}</td><td>$${(s.price||0).toFixed(2)}</td></tr>`;
    });
    wrap.innerHTML = h + '</table>';
  } catch(e) {}
}

async function loadAllSignals() {
  try {
    const r = await fetch('/api/signals?limit=200'); const d = await r.json();
    const wrap = document.getElementById('allSignalsWrap');
    if (!d.signals || !d.signals.length) {
      wrap.innerHTML = '<div style="color:#8b949e;padding:20px">No signals yet.</div>'; return;
    }
    let h = '<table class="signal-table"><tr><th>Time</th><th>Symbol</th><th>Direction</th><th>Route</th><th>TFs</th><th>RSI</th><th>MACD</th><th>Price</th><th>ATR</th></tr>';
    d.signals.forEach(s => {
      h += `<tr><td>${s.timestamp?.slice(0,19)||''}</td><td><b>${s.symbol}</b></td>
        <td><span class="badge badge-${s.direction}">${s.direction?.toUpperCase()}</span></td>
        <td><span class="badge badge-${s.route?.toLowerCase()}">${s.route}</span></td>
        <td>${s.valid_count}/4</td><td>${(s.rsi||0).toFixed(1)}</td>
        <td>${(s.macd_hist||0).toFixed(3)}</td><td>$${(s.price||0).toFixed(2)}</td><td>${(s.atr||0).toFixed(2)}</td></tr>`;
    });
    wrap.innerHTML = h + '</table>';
  } catch(e) {}
}

function colorLog(line) {
  if (line.includes('SIGNAL') || line.includes('*** ')) return `<span class="signal">${line}</span>`;
  if (line.includes('ERROR') || line.includes('error')) return `<span class="err">${line}</span>`;
  if (line.includes('WARNING')) return `<span class="warn">${line}</span>`;
  return `<span class="info">${line}</span>`;
}

async function loadLogs() {
  try {
    const r = await fetch('/api/logs?lines=80'); const d = await r.json();
    const el = document.getElementById('logbox');
    el.innerHTML = d.lines.map(colorLog).join('\n');
    el.scrollTop = el.scrollHeight;
  } catch(e) {}
}

async function loadFullLogs() {
  try {
    const r = await fetch('/api/logs?lines=300'); const d = await r.json();
    const el = document.getElementById('fullLogbox');
    el.innerHTML = d.lines.map(colorLog).join('\n');
    el.scrollTop = el.scrollHeight;
  } catch(e) {}
}

async function loadSettings() {
  try {
    const r = await fetch('/api/settings'); const d = await r.json();
    document.getElementById('settingsArea').value = d.content || '';
  } catch(e) {}
}

async function saveSettings() {
  const content = document.getElementById('settingsArea').value;
  const r = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({content})});
  const d = await r.json();
  const alert = document.getElementById('settingsAlert');
  alert.innerHTML = d.ok
    ? '<div class="alert alert-success">✅ Settings saved. Bot restarting...</div>'
    : '<div class="alert alert-error">❌ Error: ' + (d.error||'unknown') + '</div>';
  setTimeout(() => alert.innerHTML = '', 4000);
}

async function botAction(action) {
  await fetch('/api/' + action, {method:'POST'});
  setTimeout(loadStatus, 2000);
}

// Check if already logged in
fetch('/api/status').then(r => {
  if (r.ok) {
    authed = true;
    document.getElementById('loginPage').style.display = 'none';
    document.getElementById('mainApp').style.display = 'block';
    loadAll();
    setInterval(loadAll, 30000);
  } else {
    document.getElementById('loginPage').style.display = 'flex';
  }
}).catch(() => {
  document.getElementById('loginPage').style.display = 'flex';
});
</script>
</body>
</html>'''

# Flask routes
@app.route('/')
def index():
    if not is_logged_in():
        return render_template_string(HTML)
    return render_template_string(HTML)

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    if data.get('password') == get_password():
        session['authenticated'] = True
        return jsonify({'ok': True})
    return jsonify({'ok': False}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/status')
@require_auth
def status():
    import subprocess
    running = bool(subprocess.run(['pgrep', '-f', 'main.py'], capture_output=True).stdout.strip())
    sig_count = ibkr_count = etoro_count = 0
    try:
        db = sqlite3.connect(DB_PATH)
        sig_count   = db.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
        ibkr_count  = db.execute("SELECT COUNT(*) FROM signals WHERE route='IBKR'").fetchone()[0]
        etoro_count = db.execute("SELECT COUNT(*) FROM signals WHERE route='ETORO'").fetchone()[0]
        db.close()
    except: pass
    return jsonify({'running': running, 'signal_count': sig_count,
                    'ibkr_count': ibkr_count, 'etoro_count': etoro_count,
                    'win_count': 0, 'win_rate': 'N/A'})

@app.route('/api/signals')
@require_auth
def signals():
    limit = min(int(request.args.get('limit', 10)), 500)
    try:
        db = sqlite3.connect(DB_PATH)
        rows = db.execute(
            'SELECT * FROM signals ORDER BY id DESC LIMIT ?', (limit,)
        ).fetchall()
        cols = [d[0] for d in db.execute('SELECT * FROM signals LIMIT 1').description] if rows else []
        db.close()
        return jsonify({'signals': [dict(zip(cols, r)) for r in rows]})
    except Exception as e:
        return jsonify({'signals': [], 'error': str(e)})

@app.route('/api/logs')
@require_auth
def logs():
    lines = min(int(request.args.get('lines', 100)), 500)
    try:
        with open(LOG_PATH) as f:
            all_lines = f.readlines()
        return jsonify({'lines': [l.rstrip() for l in all_lines[-lines:]]})
    except:
        return jsonify({'lines': ['Log file not found']})

@app.route('/api/settings', methods=['GET', 'POST'])
@require_auth
def settings():
    if request.method == 'GET':
        try:
            with open(CONFIG_PATH) as f:
                return jsonify({'content': f.read()})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    else:
        data = request.get_json(silent=True) or {}
        content = data.get('content', '')
        try:
            yaml.safe_load(content)  # validate YAML before saving
            with open(CONFIG_PATH, 'w') as f:
                f.write(content)
            subprocess.Popen(['bash', START_SCRIPT])
            return jsonify({'ok': True})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 400

@app.route('/api/start', methods=['POST'])
@require_auth
def start():
    subprocess.Popen(['bash', START_SCRIPT])
    return jsonify({'ok': True})

@app.route('/api/stop', methods=['POST'])
@require_auth
def stop():
    subprocess.run(['pkill', '-f', 'main.py'])
    return jsonify({'ok': True})

@app.route('/api/restart', methods=['POST'])
@require_auth
def restart():
    # Download latest code directly from GitHub (bypasses git pull issues)
    def _restart():
        import time
        time.sleep(1)
        subprocess.Popen(
            ['bash', START_SCRIPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
    import threading
    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/start', methods=['POST'])
@require_auth  
def start_bot():
    subprocess.Popen(['bash', '-c', f'sleep 1 && bash {START_SCRIPT}'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return jsonify({'ok': True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
