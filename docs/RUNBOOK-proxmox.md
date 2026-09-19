# Runbook — sumar el Proxmox al WOL-Bot

Procedimiento para que el bot encienda y apague el hipervisor `pve`
(`192.168.1.70`, MAC `70:85:C2:BD:C8:53`), que vive en **otra VLAN** que el bot.

| | |
|---|---|
| Bot | `server-mbp` · `192.168.2.40` · VLAN 20 "servidores" |
| Proxmox | `pve` · `192.168.1.70` · VLAN 1 "admin" |
| Router | OpenWRT C59 · `192.168.1.1` (`br-lan`) / `192.168.2.1` (`eth0.20`) |

El código ya está desplegado; lo que sigue son los cambios de red y de host que
ese código da por hechos. **Hasta que estén aplicados y verificados, el horario
del Proxmox apaga pero no enciende.**

---

## 0. Antes de empezar: por qué hace falta cada cosa

- El magic packet por defecto va a `255.255.255.255`. Eso es *limited
  broadcast*: muere en el segmento, ningún router lo reenvía. Por eso el bot lo
  manda **unicast** a `192.168.1.70`, y por eso hace falta una **ARP
  permanente** — con la máquina apagada no hay quién responda el ARP y el
  router descarta el paquete antes de mandarlo.
- El gateway **descarta el ICMP entre VLANs** (verificable: un ping desde
  `.2.40` a `.1.70` vuelve `Destination Port Unreachable` desde `192.168.2.1`).
  Por eso la sonda de estado es TCP/22, que ya está permitido.
- El apagado entra por SSH. Se usa un **usuario dedicado con clave de comando
  forzado**, no root: si mañana se filtra el `.env` del bot, lo que gana el
  atacante es un botón de apagado, no root del hipervisor.

---

## 1. Router — ARP permanente + reglas de firewall

```sh
ssh openwrt-c59
```

### 1.1 ARP permanente (para que el paquete salga con la máquina apagada)

```sh
ip neigh replace 192.168.1.70 lladdr 70:85:c2:bd:c8:53 dev br-lan nud permanent
```

Persistirla, porque `ip neigh` no sobrevive un reboot del router:

```sh
cat >> /etc/rc.local <<'EOF'
ip neigh replace 192.168.1.70 lladdr 70:85:c2:bd:c8:53 dev br-lan nud permanent
EOF
# verificar que el exit 0 quede al final:
grep -n . /etc/rc.local
```

Verificar:

```sh
ip neigh show 192.168.1.70   # debe decir PERMANENT
```

### 1.2 Regla de firewall — WOL unicast

```sh
uci add firewall rule
uci set firewall.@rule[-1].name='Allow-WOL-mbp-to-proxmox'
uci set firewall.@rule[-1].src='VLAN'
uci set firewall.@rule[-1].src_ip='192.168.2.40'
uci set firewall.@rule[-1].dest='lan'
uci set firewall.@rule[-1].dest_ip='192.168.1.70'
uci set firewall.@rule[-1].proto='udp'
uci set firewall.@rule[-1].dest_port='9'
uci set firewall.@rule[-1].target='ACCEPT'
uci set firewall.@rule[-1].enabled='1'
uci commit firewall && /etc/init.d/firewall reload
```

> La sonda de estado **no necesita regla nueva**: reusa la ya existente
> `Allow-Ansible-mbp-to-proxmox-host` (`192.168.2.40` → `192.168.1.70` tcp/22).

**Rollback:** `uci delete firewall.@rule[-1] && uci commit firewall && /etc/init.d/firewall reload`
(verificá antes con `uci show firewall | grep -A1 Allow-WOL` que `[-1]` sea la regla correcta).

---

## 2. Proxmox — usuario dedicado para el apagado

```sh
ssh root@192.168.1.70
```

### 2.1 Usuario y wrapper de comando forzado

```sh
useradd -r -m -d /var/lib/wol-bot -s /bin/sh wol-bot

cat > /usr/local/sbin/wol-bot-power <<'EOF'
#!/bin/sh
# Ejecutado como comando forzado desde authorized_keys. El sshd ignora lo que
# mande el cliente y corre esto; el pedido real llega en $SSH_ORIGINAL_COMMAND.
case "$SSH_ORIGINAL_COMMAND" in
  halt)   exec sudo -n /sbin/shutdown -h +0 ;;
  reboot) exec sudo -n /sbin/shutdown -r +0 ;;
  *)      echo "wol-bot: comando no permitido" >&2; exit 1 ;;
esac
EOF
chmod 755 /usr/local/sbin/wol-bot-power
```

### 2.2 sudo acotado a esos dos comandos

```sh
cat > /etc/sudoers.d/wol-bot <<'EOF'
wol-bot ALL=(root) NOPASSWD: /sbin/shutdown -h +0, /sbin/shutdown -r +0
EOF
chmod 440 /etc/sudoers.d/wol-bot
visudo -c        # debe decir "parsed OK"
```

### 2.3 Clave del bot, restringida

Copiar la pública del bot (desde la máquina de trabajo):

```sh
ssh server-mbp 'cat ~/.ssh/id_ed25519_wol.pub'
```

y en el pve:

```sh
install -d -m 700 -o wol-bot -g wol-bot /var/lib/wol-bot/.ssh
cat > /var/lib/wol-bot/.ssh/authorized_keys <<'EOF'
restrict,command="/usr/local/sbin/wol-bot-power" ssh-ed25519 AAAA...PEGAR_ACA wol-bot@server-mbp
EOF
chmod 600 /var/lib/wol-bot/.ssh/authorized_keys
chown -R wol-bot:wol-bot /var/lib/wol-bot/.ssh
```

`restrict` apaga port-forwarding, agent-forwarding, X11, pty y user-rc de una.

### 2.4 Wake-on-LAN armado

Ya existe `wol-nic0.service` (enabled) que hace `ethtool -s nic0 wol g` en cada
boot. Verificar:

```sh
systemctl is-enabled wol-nic0.service   # enabled
ethtool nic0 | grep Wake-on             # Wake-on: g
```

**Rollback:** `userdel -r wol-bot; rm -f /etc/sudoers.d/wol-bot /usr/local/sbin/wol-bot-power`

---

## 3. Verificación (en este orden)

### 3.1 Sonda de estado

```sh
ssh server-mbp 'cd /home/luca/discord-wake-on-lan && ./venv/bin/python -c "
import asyncio, network; print(asyncio.run(network.probe(\"proxmox\")))"'
# → True
```

### 3.2 El magic packet llega de verdad

Con el pve **encendido** (el paquete es inofensivo). En una terminal:

```sh
ssh root@192.168.1.70 'timeout 30 tcpdump -ni nic0 udp port 9 -c 1'
```

y en otra:

```sh
ssh server-mbp 'wakeonlan -i 192.168.1.70 -p 9 70:85:C2:BD:C8:53'
```

El `tcpdump` tiene que capturar el paquete. **Si no lo captura, pará acá**: el
encendido no va a funcionar y no conviene seguir.

### 3.3 Apagado por la clave restringida

```sh
# debe fallar (la clave no ejecuta comandos arbitrarios):
ssh server-mbp 'ssh -i ~/.ssh/id_ed25519_wol wol-bot@192.168.1.70 "id"'
# → wol-bot: comando no permitido
```

### 3.4 Ciclo completo — el único test que cuenta

Apagar y despertar el pve una vez a mano, **antes** de la primera ventana de las
23:00. Elegí un horario en que bajar los huéspedes no moleste.

```sh
ssh server-mbp 'ssh -i ~/.ssh/id_ed25519_wol wol-bot@192.168.1.70 halt'
# esperar ~60s, confirmar que dejó de responder, y:
ssh server-mbp 'wakeonlan -i 192.168.1.70 -p 9 70:85:C2:BD:C8:53'
```

Si vuelve solo, el sistema está listo. Si **no** vuelve, lo más probable es que
la NIC pierda el armado de WOL en el camino de apagado (el `r8169` es conocido
por eso): armar con `ethtool -s nic0 wol g` también en un
`ExecStop`/`systemd-shutdown` hook y repetir la prueba.

---

## 4. Lo que hay que tener presente al operar esto

Apagar el hipervisor baja **todo** lo que hostea:

| VMID | Qué es | `qemu-guest-agent` | Cómo lo apaga el host |
|---|---|---|---|
| 100 | `vm-monitoring-debian` (Prometheus/Grafana) | ✗ | ACPI — Debian lo honra |
| 101 | `alpine-monitoring` (LXC) | — | baja limpio |
| 102 | `win2008r2-sql` | ✗ | **ACPI a un Windows Server con SQL** |
| 103 | `tailscale-alpine` | ✓ | por agente |

- **El 102 es el riesgo real de un ciclo diario.** Sin guest agent el host le
  manda ACPI; si Windows no baja dentro del timeout de `pve-guests`, el ciclo
  termina siendo un corte duro. Antes de confiar en el horario, hacé el ciclo
  de 3.4 y mirá cómo se comporta esa VM.
- Mientras esté apagado **no hay Grafana/Prometheus ni acceso por Tailscale**.
- El **backup orquestado corre a las 08:00** y lee `/etc/pve` del `.1.70`. La
  franja abre a las 08:00: si adelantás el apagado o atrasás el encendido,
  fijate de no dejar el backup afuera.

## 5. Pendiente detectado, ajeno a este cambio

`homelab-backup` lee `/etc/pve` como `ansible-pct@192.168.1.70`, pero **ese
usuario no existe en el pve** (`grep ansible-pct /etc/passwd` no devuelve
nada). Encaja con el síntoma ya conocido de que `/etc/pve` se respalda vacío.
