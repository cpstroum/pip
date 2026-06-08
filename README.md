# pip
A Unihiker emotional support solution for kids

## Deploying to the device

The Unihiker connects via USB and is reachable at `10.1.2.3` (default credentials: `root` / `dfrobot`).

Copy all project files to the device with `scp`:

```bash
scp pip.py requirements.txt setup.sh *.png .env root@10.1.2.3:/root/pip/
```

Then SSH in and run setup:

```bash
ssh root@10.1.2.3
cd /root/pip
bash setup.sh
python pip.py
```

To copy a single file (e.g. after a code update):

```bash
scp pip.py root@10.1.2.3:/root/pip/
```
