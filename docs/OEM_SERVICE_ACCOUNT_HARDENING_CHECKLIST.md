# OEM and Service Account Hardening Checklist

- [ ] Inventory every OEM, HMI, camera, database, Windows, service, and remote
  support account without copying credentials into this record.
- [ ] Confirm default and empty passwords are disabled where the vendor permits.
- [ ] Record the OEM approval/ticket for any account, interlock, or safety-setting
  change; do not alter unsupported safety/service settings.
- [ ] Restrict management interfaces to the approved management VLAN and jump
  host; block them from equipment and enterprise user networks by default.
- [ ] Give the middleware service only its documented virtual service identity
  and runtime-directory access. Review effective ACLs, not just intended grants.
- [ ] Enable privileged-access logging, time synchronization, named operator
  accounts, periodic access review, and break-glass custody.
- [ ] Record equipment owner, OEM approver, date, model/software revision,
  deviations, compensating controls, and evidence location.
