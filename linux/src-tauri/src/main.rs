// Bez dodatkowego okna konsoli na Windowsie (na Linuksie bez znaczenia).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    synclocal360_galeria_lib::run()
}
