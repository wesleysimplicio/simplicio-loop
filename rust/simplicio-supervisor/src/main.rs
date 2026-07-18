use std::io::{self, Read, Write};

use simplicio_supervisor::{run, ProcessSpec};

#[tokio::main]
async fn main() {
    let mut raw = String::new();
    if let Err(err) = io::stdin().read_to_string(&mut raw) {
        eprintln!("simplicio-supervisor: failed to read stdin: {err}");
        std::process::exit(2);
    }

    let spec: ProcessSpec = match serde_json::from_str(&raw) {
        Ok(spec) => spec,
        Err(err) => {
            eprintln!("simplicio-supervisor: invalid ProcessSpec JSON: {err}");
            std::process::exit(2);
        }
    };

    let result = run(&spec).await;
    let body = serde_json::to_string(&result).expect("ProcessResult always serializes");
    let mut stdout = io::stdout();
    let _ = stdout.write_all(body.as_bytes());
    let _ = stdout.write_all(b"\n");
}
