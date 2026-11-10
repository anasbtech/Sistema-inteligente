"""Modelos de veículo comuns na frota brasileira, para o reconhecimento por CLIP.

Lista curada (mais vendidos e mais presentes na frota de 2005 em diante). O
classificador só pode responder o que está aqui: um modelo fora da lista sai
como "não identificado", ou, se for parecido com um da lista, com confiança
baixa — e aí também não é afirmado. Acrescentar um modelo é acrescentar uma
linha; depois, rodar a validação (scripts/veiculos_validar.py).
"""
from __future__ import annotations

# (marca, modelo, categoria). Categoria: carro | moto
MODELOS = [
    ("Chevrolet", "Onix", "carro"), ("Chevrolet", "Onix Plus", "carro"), ("Chevrolet", "Prisma", "carro"),
    ("Chevrolet", "Celta", "carro"), ("Chevrolet", "Corsa", "carro"), ("Chevrolet", "Cruze", "carro"),
    ("Chevrolet", "Tracker", "carro"), ("Chevrolet", "S10", "carro"), ("Chevrolet", "Spin", "carro"),
    ("Chevrolet", "Montana", "carro"), ("Chevrolet", "Cobalt", "carro"), ("Chevrolet", "Agile", "carro"),
    ("Volkswagen", "Gol", "carro"), ("Volkswagen", "Voyage", "carro"), ("Volkswagen", "Fox", "carro"),
    ("Volkswagen", "Polo", "carro"), ("Volkswagen", "Virtus", "carro"), ("Volkswagen", "T-Cross", "carro"),
    ("Volkswagen", "Nivus", "carro"), ("Volkswagen", "Saveiro", "carro"), ("Volkswagen", "Amarok", "carro"),
    ("Volkswagen", "Up", "carro"), ("Volkswagen", "Kombi", "carro"), ("Volkswagen", "Jetta", "carro"),
    ("Volkswagen", "Taos", "carro"),
    ("Fiat", "Uno", "carro"), ("Fiat", "Palio", "carro"), ("Fiat", "Siena", "carro"), ("Fiat", "Strada", "carro"),
    ("Fiat", "Toro", "carro"), ("Fiat", "Mobi", "carro"), ("Fiat", "Argo", "carro"), ("Fiat", "Cronos", "carro"),
    ("Fiat", "Pulse", "carro"), ("Fiat", "Fastback", "carro"), ("Fiat", "Fiorino", "carro"),
    ("Fiat", "Ducato", "carro"), ("Fiat", "Doblo", "carro"), ("Fiat", "Punto", "carro"),
    ("Hyundai", "HB20", "carro"), ("Hyundai", "HB20S", "carro"), ("Hyundai", "Creta", "carro"),
    ("Hyundai", "Tucson", "carro"), ("Hyundai", "ix35", "carro"), ("Hyundai", "HR", "carro"),
    ("Toyota", "Corolla", "carro"), ("Toyota", "Corolla Cross", "carro"), ("Toyota", "Etios", "carro"),
    ("Toyota", "Yaris", "carro"), ("Toyota", "Hilux", "carro"), ("Toyota", "SW4", "carro"),
    ("Honda", "Civic", "carro"), ("Honda", "City", "carro"), ("Honda", "Fit", "carro"),
    ("Honda", "HR-V", "carro"), ("Honda", "WR-V", "carro"),
    ("Renault", "Kwid", "carro"), ("Renault", "Sandero", "carro"), ("Renault", "Logan", "carro"),
    ("Renault", "Duster", "carro"), ("Renault", "Oroch", "carro"), ("Renault", "Captur", "carro"),
    ("Renault", "Master", "carro"),
    ("Ford", "Ka", "carro"), ("Ford", "Fiesta", "carro"), ("Ford", "EcoSport", "carro"),
    ("Ford", "Ranger", "carro"), ("Ford", "Focus", "carro"),
    ("Nissan", "Kicks", "carro"), ("Nissan", "Versa", "carro"), ("Nissan", "March", "carro"),
    ("Nissan", "Frontier", "carro"),
    ("Jeep", "Renegade", "carro"), ("Jeep", "Compass", "carro"), ("Jeep", "Commander", "carro"),
    ("Peugeot", "208", "carro"), ("Peugeot", "2008", "carro"), ("Citroën", "C3", "carro"),
    ("Citroën", "C4 Cactus", "carro"), ("Citroën", "Aircross", "carro"),
    ("Mitsubishi", "L200", "carro"), ("Mitsubishi", "Outlander", "carro"),
    ("Caoa Chery", "Tiggo 5x", "carro"), ("Caoa Chery", "Tiggo 7", "carro"),
    ("BYD", "Dolphin", "carro"), ("BYD", "Song Plus", "carro"),
    ("Mercedes-Benz", "Sprinter", "carro"),
    ("Honda", "CG 160", "moto"), ("Honda", "Biz", "moto"), ("Honda", "Pop 110i", "moto"),
    ("Honda", "CB 300F Twister", "moto"), ("Honda", "NXR 160 Bros", "moto"), ("Honda", "PCX", "moto"),
    ("Honda", "XRE 300", "moto"), ("Honda", "Elite 125", "moto"),
    ("Yamaha", "Factor 150", "moto"), ("Yamaha", "Fazer 250", "moto"), ("Yamaha", "Crosser 150", "moto"),
    ("Yamaha", "Lander 250", "moto"), ("Yamaha", "NMax", "moto"), ("Yamaha", "MT-03", "moto"),
]
