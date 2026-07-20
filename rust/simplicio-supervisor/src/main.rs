use std::io::{self, Read, Write};

use simplicio_supervisor::{parse_spec, run, serialize_result};

#[tokio::main]
async fn main() {
    let mut raw = String::new();
    if let Err(err) = io::stdin().read_to_string(&mut raw) {
        eprintln!("simplicio-supervisor: failed to read stdin: {err}");
        std::process::exit(2);
    }

    let spec = match parse_spec(&raw) {
        Ok(spec) => spec,
        Err(err) => {
            eprintln!("simplicio-supervisor: invalid ProcessSpec JSON: {err}");
            std::process::exit(2);
        }
    };

    let result = run(&spec).await;
    let body = serialize_result(&result);
    let mut stdout = io::stdout();
    let _ = stdout.write_all(body.as_bytes());
    let _ = stdout.write_all(b"\n");
}
