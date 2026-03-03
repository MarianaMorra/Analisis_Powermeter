import os
import pandas as pd
from .utilities_menu.funciones import CONFIG
from .utilities_menu.funciones import modificar_parametros_eventos_menu
from .utilities_menu.funciones import calcular_eventos_desde_df
from .utilities_menu.funciones import cargar_csv_instantanea
from .utilities_menu.funciones import graficar_maquina
from .utilities_menu.funciones import exportar_config
from .utilities_menu.funciones import cargar_ultimo_config_maquina
from .utilities_menu.funciones import eliminar_eventos_solapados

def mostrar_menu():
    print("\n--- Menú de opciones ---")
    print("1. Ingresar datos")
    print("2. Modificar parametros")
    print("3. Graficar")
    print("4. Exportar cambios")
    print("0. Salir")
    print("-------------------------")

def main():
    df_i = None              # DF filtrado a la máquina elegida
    df_ev = None             # DF eventos de la máquina elegida (inicio/fin)
    serie = None
    nombre = None

    while True:
        mostrar_menu()
        opcion = input("Seleccione una opción: ").strip()

        # --- INGRESO DE DATOS ---------------------------------------------------------
        if opcion == "1":
            try:
                path_csv = input("Ingrese el nombre o path del archivo CSV: ").strip()

                if not path_csv:
                    print("No se ingresó archivo.")
                    continue

                if not os.path.exists(path_csv):
                    print("El archivo no existe.")
                    continue

                df_i, serie, nombre = cargar_csv_instantanea(path_csv)
                print(f"CSV cargado correctamente. Filas: {len(df_i)}")
                
                # cargar última config de ESA máquina
                cargar_ultimo_config_maquina(nombre)

                # cuando cambio de máquina, invalido eventos previos
                df_ev = None
                
                print(f"Máquina seleccionada: {nombre} ({serie}). Filas: {len(df_i)}")

            except Exception as e:
                print(f"Error cargando CSV: {e}")

        # --- MODIFICACION PARAMETROS -----------------------------------------------
        elif opcion == "2":
            if df_i is None or df_i.empty:
                print("Primero cargue el CSV y seleccione la máquina (opción 1).")
                continue
    
            try:
                modificar_parametros_eventos_menu()
            except Exception as e:
                print(f"Error modificando parámetros: {e}")

        # --- GRAFICA CORRIENTES ----------------------------------------------------
        elif opcion == "3":
            if df_i is None or df_i.empty:
                print("Primero cargue el CSV y seleccione la máquina (opción 1).")
                continue

            try:
                eventos = calcular_eventos_desde_df(df_i, **CONFIG)

                # filtrar eventos válidos
                eventos = [
                    {"inicio": e["inicio"], "fin": e["fin"]}
                    for e in eventos
                    if e.get("inicio") is not None and e.get("fin") is not None
                ]

                # eliminar solapados
                eventos_maquina = eliminar_eventos_solapados(eventos)

                print(f"Eventos calculados: {len(eventos_maquina)}")

                graficar_maquina(df_i, serie, nombre, eventos_maquina, "salidas")

            except Exception as e:
                print(f"Error calculando eventos o graficando: {e}")

        elif opcion == "4":
            try:
                path = exportar_config(nombre)
                print(f"OK. Config exportada a: {path}")
            except Exception as e:
                print(f"Error exportando config: {e}")

        elif opcion == "0":
            print("Saliendo...")
            break

        else:
            print("Opción inválida.")


if __name__ == "__main__":
    main()