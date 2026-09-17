# Isolate browser identity and authorize model disclosure

Runs default to a dedicated QA browser profile; access to an existing profile requires explicit opt-in and never silently copies personal cookies. Each project separately authorizes content disclosure to hosted models, with unapproved content denied by default; removing known secrets alone does not authorize disclosure. The initial demonstration uses a separate synthetic todo application and excludes all proprietary applications and their code, data, screenshots, and workflows.
