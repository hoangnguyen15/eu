FLOW AGENT - HUONG DAN CAI DAT TREN MAY TRAM (Windows Server 2022 / Windows 10-11 x64)
=====================================================================================

Can co:
  * Quyen Administrator tren may tram.
  * May tram ket noi duoc toi may chu: cong 8443 (gateway mTLS) va 9000 (luu tru anh).
  * File flowagent-config.json tao tu trang quan tri.

Buoc 1 - Tren trang quan tri (http://<may-chu>:8000/admin/):
  1. Muc "Phien dang nhap": them tai khoan provider ma may nay se dung va ghi can cu
     duoc phep su dung (neu chua co).
  2. Muc "Them may tram": nhap ten may, so slot, chon tai khoan + profile ref,
     bam "Tao cau hinh cai dat". Trinh duyet tai ve flowagent-config-<ten>.json.
     File nay chua token dung MOT LAN, het han sau 24 gio.

Buoc 2 - Tren may tram:
  1. Giai nen goi FlowAgent-<phien-ban>-win-x64.zip vao mot thu muc, vi du C:\Temp\FlowAgent
  2. Chep file cau hinh vao cung thu muc va DOI TEN thanh: flowagent-config.json
  3. Chuot phai Install-FlowAgent.cmd -> Run as administrator.
  4. Sau vai giay, may tram hien ONLINE o muc "May tram" tren trang quan tri.

Dang nhap provider (vi du Google Flow) tren may tram:
  * Nguoi van hanh tu dang nhap tren chinh may tram, trong profile tuong ung.
  * He thong KHONG luu mat khau, KHONG doc/chuyen cookie hay session token,
    KHONG tu dong dang nhap.
  * Dang nhap xong, vao trang quan tri bam "Kiem tra lai phien".
  * Luu y: connector Google Flow dang DISABLED cho den khi co phuong thuc tich hop
    duoc Google cho phep. MockImageProvider dung de chay thu toan bo quy trinh.

Kiem tra / xu ly su co:
  * Nhat ky: Event Viewer -> Windows Logs -> Application (nguon: FlowAgent).
  * Trang thai dich vu: sc query FlowAgent
  * Kiem tra file cau hinh ma khong cai dat:
      powershell -ExecutionPolicy Bypass -File install.ps1 -ConfigFile flowagent-config.json -DryRun
  * Go cai dat: Uninstall-FlowAgent.cmd (giu danh tinh), hoac
      uninstall.ps1 -PurgeData   (xoa ca chung chi + hang doi su kien cuc bo)
  * Thu hoi may tram tu xa: trang quan tri -> May tram -> "Vo hieu hoa".
