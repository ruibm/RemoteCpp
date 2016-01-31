# RemoteCpp Sublime Text Plugin

The simple goal of this Sublime Text Editor plugin is to make it pleasant/acceptable to develop C++ remotely via SSH.

<img src='https://raw.githubusercontent.com/ruibm/RemoteCpp/master/ScreenCasts/RemoteCpp.gif' alt='RemoteCpp.gif' style="border: 1px solid #b3b3b3" />

## Key Shortcuts/Features

* **Ctrl+Cmd+Alt+L**: List Remote Files.
* **Cmd+Alt+L**: List Remote Files In Current Open Path.
* **Cmd+Enter**: Goto #include'd Remote File.
* **Cmd+Alt+Up**: Toggle Header/Implementation Remote File.
* **Cmd+Alt+B**: Remote Build.
* **Cmd+Alt+N**: New Remote File.
* **Cmd+Alt+O**: Open Remote File.
* **Cmd+Alt+M**: Move Remote File In Current View.
* **Cmd+Alt+D**: Delete Remote File In Current View.
* **Cmd+Alt+R**: Refresh Current View.
* **Ctrl+Cmd+Alt+O**: Quick Open File.
* **Ctrl+Cmd+Alt+R**: Refresh All Views.
* **Ctrl+Cmd+Alt+G**: Grep All Remote Files.


### View Specific Key Shortcuts/Features
* **Enter** *(In Build View)*: Goto Build Error File Under the Cursor.
* **Enter** *(In Grep View)*: Goto File Matched By Grep Under the Cursor.
* **Enter** *(In ListFiles View)*: Open File Under Cursor.


## How Does It Work?

RemoteCpp relies on the ability to communicate with the remote host without having to manually type the password every single time.
To do so there are at least two options:

1. Configure the remote ssh server to accept you ssh key (by updating the ~/.ssh/auhtorized_keys file). Here's a link on how to achieve this:
https://www.debian.org/devel/passwordlessssh

2. Create a SSH listen tunnel to the remote server so RemoteCpp always connects to localhost port ["remote_cpp_ssh_port": "8888"]. This way you only have to type your ssh password once. Here's an example of the command you can use to connect:

```
ssh -L 8888:localhost:22 $REMOTE_HOSTNAME -o TCPKeepAlive=yes \
  -o ServerAliveCountMax=1000000 -o ServerAliveInterval=300 \
  -o ControlPersist=4h -o ControlMaster=yes \
  'while true; do echo "=> [$(date)] Still Alive!!! :)" ; sleep 5s; done;'
```


## Requirements

RemoteCpp relies on some Unix command line tools to be in $PATH in order to run correctly.
Apart from *ssh* which is required locally, all these tools should be available in the remote machine.
Here is a list of some of the used tools:

* find
* grep
* mkdir
* mv
* rm
* scp
* ssh

If some particular RemoteCpp command does not seem to work please take a look at Sublime Text Console (key shortcut is **Ctrl+`**) to diagnose.


## Contacts and Bug Reports
1. Via BitBucket: https://bitbucket.org/ruibm/remotecpp
2. Via Email: ruibm@ruibm.com


## License

RemoteCpp is release under the license: Apache License Version 2.0, January 2004
For full details please read: https://bitbucket.org/ruibm/remotecpp/src/master/LICENSE
