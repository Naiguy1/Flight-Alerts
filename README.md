# Flight-Alerts

Flight-Alerts is a tool that helps you find cheap flights by watching prices and sending you email alerts when a fare meets your deal settings. You can run it on your own computer or server, and manage it through a password-protected website.

## Getting Started

To get started with Flight-Alerts, you'll need to follow these steps:

### Step 1: Install Git and Docker

Before you can install Flight-Alerts, you'll need to install Git and Docker on your computer. Here's how:

* **Windows:**
	1. Download and install [Git for Windows](https://gitforwindows.org/).
	2. Download and install [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop).
* **Mac:**
	1. Open the Terminal app on your Mac. You can find it in the Applications/Utilities folder, or use Spotlight to search for it.
	2. Run the following command to install Git: `brew install git`
	3. Run the following command to install Docker: `brew install --cask docker`
* **Linux:**
	1. Open the Terminal app on your Linux computer.
	2. Run the following command to install Git: `sudo apt-get install git`
	3. Run the following command to install Docker: `sudo apt-get install docker.io`

### Step 2: Clone the Flight-Alerts Repository

Once you have Git and Docker installed, you can clone the Flight-Alerts repository. Here's how:

* **Windows:**
	1. Open the Git Bash terminal.
	2. Run the following command: `git clone https://github.com/Naiguy1/Flight-Alerts.git`
* **Mac/Linux:**
	1. Open the Terminal app.
	2. Run the following command: `git clone https://github.com/Naiguy1/Flight-Alerts.git`

### Step 3: Create a Docker Compose File

Next, you'll need to create a Docker Compose file to configure your Flight-Alerts installation. Here's how:

* **All platforms:**
	1. Open the Terminal app.
	2. Navigate to the Flight-Alerts directory: `cd Flight-Alerts`
	3. Run the following command to create a Docker Compose file: `cp docker-compose.yml.example docker-compose.yml`

### Step 4: Configure Your Flight-Alerts Installation

Now you'll need to configure your Flight-Alerts installation. Here's how:

* **All platforms:**
	1. Open the `docker-compose.yml` file in a text editor.
	2. Update the `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, and `SMTP_PASSWORD` variables to match your email settings.
	3. Update the `FARE_PROVIDER_API_KEY` variable to match your fare provider API key.
	4. Save the file.

### Step 5: Start Flight-Alerts

Finally, you can start Flight-Alerts. Here's how:

* **All platforms:**
	1. Open the Terminal app.
	2. Navigate to the Flight-Alerts directory: `cd Flight-Alerts`
	3. Run the following command to start Flight-Alerts: `docker compose up -d`

That's it! Flight-Alerts should now be up and running. You can access the dashboard by visiting `http://localhost:8080` in your web browser.

## Troubleshooting

If you encounter any issues during the setup process, here are some troubleshooting tips:

* Make sure you have the latest version of Git and Docker installed.
* Check that you have cloned the Flight-Alerts repository correctly.
* Verify that your Docker Compose file is configured correctly.
* Check the Flight-Alerts logs for any error messages.

## Further Reading

For more information on Flight-Alerts, including technical details and advanced configuration options, please see the following documents:

* [Architecture and limitations](docs/architecture.md)
* [Production readiness](docs/production-readiness.md)
* [Maintainer merge procedure](docs/merging.md)
* [Security policy](SECURITY.md)

We hope this helps! If you have any questions or need further assistance, don't hesitate to ask.
