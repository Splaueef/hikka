<div align="center">

# Hikka (Custom Build)

<img src="https://img.shields.io/badge/status-unofficial%20fork-orange">
<img src="https://img.shields.io/badge/build-custom-blue">
<img src="https://img.shields.io/badge/python-3.11+-blue">

<br>

<a href="https://deepsource.io/gh/Splaueef/Hikka/?ref=repository-badge">
<img src="https://deepsource.io/gh/Splaueef/Hikka.svg/?label=active+issues&show_trend=true&token=IPVI_QX-cSuQSVeVl8cb5PLt" alt="DeepSource">
</a>

<a href="https://deepsource.io/gh/Splaueef/Hikka/?ref=repository-badge">
<img src="https://deepsource.io/gh/Splaueef/Hikka.svg/?label=resolved+issues&show_trend=true&token=IPVI_QX-cSuQSVeVl8cb5PLt" alt="DeepSource">
</a>

<br>

<a href="https://www.codacy.com/gh/Splaueef/Hikka/dashboard">
<img src="https://app.codacy.com/project/badge/Grade/97e3ea868f9344a5aa6e4d874f83db14"/>
</a>

<img src="https://img.shields.io/github/languages/code-size/Splaueef/Hikka"/>
<img src="https://img.shields.io/github/issues-raw/Splaueef/Hikka"/>
<img src="https://img.shields.io/github/license/Splaueef/Hikka"/>
<img src="https://img.shields.io/github/commit-activity/m/Splaueef/Hikka"/>

<br>

<img src="https://img.shields.io/github/forks/Splaueef/Hikka?style=flat"/>
<img src="https://img.shields.io/github/stars/Splaueef/Hikka"/>

<a href="https://github.com/psf/black">
<img src="https://img.shields.io/badge/code%20style-black-000000.svg">
</a>

</div>

---

## Notice

This repository is **NOT an official build of Hikka**.

It is a **"fork"** (the word is intentionally in quotes), because the repository was **not created using GitHub's fork system**.  
Instead, the original repository was **copied manually and uploaded as a separate project**.

The purpose of this repository is to provide a **personal modified build** with various adjustments and improvements for convenience.

### Important clarification

• I **do NOT claim ownership** of the original Hikka project  
• All **core credits belong to the original developers and contributors**  
• This repository exists only as a **custom distribution**

This repository may include:

• configuration changes  
• modified links and resources  
• custom fixes  
• experimental patches  
• personal improvements  

Over time, this repository may also include **independent updates that do not exist in the original project**.

If you want the **official project**, please refer to the original Hikka sources.


This project is a modified version of Hikka.
Original project: https://github.com/hikariatama/Hikka
Licensed under AGPL-3.0.


 The original Hikka project is currently archived and no longer actively maintained.
 This repository exists to keep the project usable and functional. 



---

## Warning

If you are a paranoid person, you should not use this userbot.

This userbot is not a virus, but it can be used for malicious purposes.  
You are responsible for all actions taken by your account.

---

## Installation

### Installation page

<img src="https://github.com/Splaueef/assets/raw/main/install_qr.gif" height="256">

<a href="https://t.me/lavhostbot?start=SGlra2E">
<img src="https://user-images.githubusercontent.com/36935426/167272288-85f00779-4b98-47da-8d0d-ea2c6370b979.png" height="40">
</a>

---

### Manual installation

Python 3.11 or newer is required.

```bash
apt update && apt install git libcairo2 -y
git clone https://github.com/Splaueef/hikka
cd Hikka
pip install -r requirements.txt
python3 -m hikka
```

### Docker installation

Docker Engine with the Compose plugin must be installed and running. Clone the
repository and start Hikka through the provided script:

```bash
git clone https://github.com/Splaueef/hikka.git
cd hikka
./docker.sh
```

Docker Compose also starts a private Redis service and configures Hikka to use
it for its database and database backups. Redis data is persisted in the
`redis` Docker volume and is not exposed outside the Compose network. If Redis
is temporarily unavailable, backups automatically fall back to the private
`hikka-backups` Telegram channel.

The application checkout and dynamically installed Python packages live in the
`worker` volume. On every container start Hikka pulls a fast-forward update and
installs its current requirements. This means `.update`, downloaded modules,
sessions, and module dependencies survive container recreation; neither
`docker compose up --build` nor an automatic application update resets them.
Temporary files used to unpack and build module dependencies are also placed on
this volume instead of the size-limited `/tmp` filesystem, allowing larger
Python packages to be installed at runtime.

The script builds the image, starts the container, and waits for the temporary
HTTPS login link. Open the address shown after `Remote setup`; a valid address
has the following form:

```text
https://<random-name>.lhr.life
```

Do **not** use `https://admin.localhost.run`: it is a localhost.run service
page, not the Hikka login page. If the `lhr.life` address is not printed within
30 seconds, keep watching the container output until the tunnel starts:

```bash
docker compose logs --follow worker
```

The tunnel requires an outbound SSH connection to `localhost.run` on port 22.
For setup on the same machine, the local page remains available at
`http://127.0.0.1:3429` by default. To select another local port, run, for
example, `EXTERNAL_PORT=8080 ./docker.sh`.
