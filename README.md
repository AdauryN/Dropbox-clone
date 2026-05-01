## Project Overview

This project is a simplified cloud storage service built on Google App Engine. It allows users to log in with their google account and manage files and folders stored in the cloud. Users can create directories, upload and download files, share files with other accounts, and view duplicate files across their entire storage. 
The application was built using Python on the backend, plain HTML and JavaScript on the frontend, and Google Cloud services for data and file storage.

## Architecture Overview

  The system is made up of three main parts: the frontend, the backend, and the cloud services. The frontend is a single HTML page that runs in the browser and communicates with the backend through standard HTTP requests. The backend is a Python server that handles all requests, enforces authentication, and manages data.The actual file content is stored in Google Cloud Storage, while metadata such as file names, directory paths, and ownership is stored in Firestore.
The frontend never communicates directly with Firestore or Cloud Storage. All operations go through the Python backend, which verifies the user's identity before doing anything.

  Browser (HTML/JS) -> Python Backend (Flask) -> Firestore (metadata) -> Google Cloud Storage (file content)

## Data Model

The application uses three Firestore collections to store all of its data.
