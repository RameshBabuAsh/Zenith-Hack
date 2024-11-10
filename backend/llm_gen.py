import os
from flask import jsonify, request
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# Configure the API key
genai.configure(api_key=os.environ['api_key'])

def generate_output():

    # Initialize the Generative Model
    model = genai.GenerativeModel(model_name='gemini-1.5-pro')

    # context
    context = """

    The RAMSharing class is designed to manage and execute functions in a distributed manner across multiple devices in a network. It enables parallel processing by discovering available devices with sufficient RAM and optionally checking for the availability of Flask servers on these devices. Once suitable devices are identified, the class allows distributed execution of functions on these devices by partitioning inputs and handling data transfer and function execution over the network.

The class provides key methods:

get_available_devices: Scans the network or a given IP list to identify devices meeting RAM and server availability requirements.
run_distributed: Distributes function execution across devices. It serializes the function and partitions input data based on the number of available devices.
_split_input: Divides input data into chunks for parallel processing, supporting lists, sets, and dictionaries.
_split_matrices: Specifically splits matrices for distributed matrix operations, dividing them into submatrices.
_combine_results: Merges distributed results (e.g., submatrices) into a single output, useful for tasks like matrix multiplication.
_send_task_to_device: Sends a function and data to a specified device, handling network requests and retries.
_check_flask_server and _check_ram: Check if the target device is suitable for function execution based on RAM availability and server readiness.
The class is utilized in two main scenarios:

Complete Parallelism (Sum of Squares Calculation): The function calculate_square_sum calculates the sum of squares of a list of numbers distributed across devices. Each device processes a subset, and the results are summed.
Partial Parallelism (Matrix Multiplication): The matrix_multiply function performs distributed matrix multiplication, where matrices are split and distributed for parallel processing. Results are combined into a final matrix output.
This setup facilitates efficient parallel execution for computational tasks across a distributed network, allowing dynamic device management and scalable processing capabilities.

Class Implementation:
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import cloudpickle
from scan_network import scan_network
import math
import numpy as np
import time
from threading import Lock

class RAMSharing:
    def __init__(self, port=5000, endpoint="/execute_function", max_retries=3):
        self.default_port = port
        self.endpoint = endpoint
        self.shared_cache = {}  # Cache to store intermediate results
        self.cache_lock = Lock()  # Lock to manage access to shared cache
        self.max_retries = max_retries  # Maximum number of retries for error handling
   

    def get_available_devices(self, ip_list=None , ram_min=1, check_server=True, max_devices=100):
        available_devices = []
        if ip_list is None:
            devices = scan_network()
            for device in devices:
                if len(available_devices) > max_devices:
                    break
                if device['available_ram'] >= ram_min*1024*1024*1024:
                    if check_server and self._check_flask_server(device['ip']):
                        available_devices.append(device['ip'])
                    elif not check_server:
                        available_devices.append(device['ip'])

        else:
            for ip in ip_list:
                if len(available_devices) > max_devices:
                    break
                if self._check_ram(ip) >= ram_min*1024*1024*1024:
                    if check_server and self._check_flask_server(ip):
                        available_devices.append(ip)
                    elif not check_server:
                        available_devices.append(ip)

        return available_devices

    def run_distributed(self, func, inputs, devices, endpoint=None, np_matrix=False, partial_parallel=False):
        endpoint = endpoint or self.endpoint
        serialized_func = self._serialize_function(func)
        results = []

        input_splits = None
        a_splits = None
        b_splits = None

        # Split matrix A and B into submatrices based on the number of devices
        if np_matrix:
            matrix_a = inputs[0]
            matrix_b = inputs[1]
            a_splits, b_splits = self._split_matrices(matrix_a, matrix_b, len(devices))
        else:
            # Split the input data based on the number of devices
            input_splits = self._split_input(inputs, len(devices))

        # Use ThreadPoolExecutor to send tasks to devices in parallel
        with ThreadPoolExecutor() as executor:
            futures = []
            
            # Determine which input data to use based on the presence of input_splits
            if input_splits is not None:
                # If input_splits is provided, use it for each device
                for device, split in zip(devices, input_splits):
                    futures.append(
                        executor.submit(self._send_task_to_device, device, serialized_func, split, endpoint, np_matrix)
                    )
            else:
                # Otherwise, use (a_splits, b_splits) for each device
                for device, (a_split, b_split) in zip(devices, zip(a_splits, b_splits)):
                    futures.append(
                        executor.submit(self._send_task_to_device, device, serialized_func, (a_split, b_split), endpoint, np_matrix)
                    )

            # Gather results as each task completes
            for future in as_completed(futures):
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    print(f"Task failed: {e}")

        return results

    def _split_input(self, inputs, num_splits):
        if isinstance(inputs, list):
            # Split list into even chunks
            chunk_size = math.ceil(len(inputs) / num_splits)
            return [inputs[i:i + chunk_size] for i in range(0, len(inputs), chunk_size)]
        
        elif isinstance(inputs, set):
            # Convert to list, split, and convert chunks back to sets
            input_list = list(inputs)
            chunk_size = math.ceil(len(input_list) / num_splits)
            return [set(input_list[i:i + chunk_size]) for i in range(0, len(input_list), chunk_size)]
        
        elif isinstance(inputs, dict):
            # Split dictionary items into even chunks
            items = list(inputs.items())
            chunk_size = math.ceil(len(items) / num_splits)
            return [dict(items[i:i + chunk_size]) for i in range(0, len(items), chunk_size)]
        
    def _split_matrices(self, matrix_a, matrix_b, num_splits):
        split_size = len(matrix_a) // int(math.sqrt(num_splits))  # assume num_splits is a perfect square
        a_splits = [matrix_a[i:i + split_size, j:j + split_size]
                    for i in range(0, len(matrix_a), split_size)
                    for j in range(0, len(matrix_a), split_size)]
        b_splits = [matrix_b[i:i + split_size, j:j + split_size]
                    for i in range(0, len(matrix_b), split_size)
                    for j in range(0, len(matrix_b), split_size)]
        return a_splits, b_splits

    def _combine_results(self, results, size):
        split_size = int(size / math.sqrt(len(results)))
        final_matrix = np.zeros((size, size))
        idx = 0
        for i in range(0, size, split_size):
            for j in range(0, size, split_size):
                final_matrix[i:i + split_size, j:j + split_size] = results[idx]
                idx += 1
        return final_matrix

    def _send_task_to_device(self, device, serialized_func, inputs, endpoint, np_matrix):
        for attempt in range(self.max_retries):
         if np_matrix:
            # Convert matrices to lists for JSON serialization
             input_lists = [mat.tolist() if isinstance(mat, np.ndarray) else mat for mat in inputs]
            
             response = requests.post(
                f"http://{device}:{self.default_port}{endpoint}",
                json={"func": serialized_func, "inputs": input_lists, "matrix": True,"use_cache": True}
             )
             response.raise_for_status()

            # Convert the result back to ndarray
             result_list = response.json().get("result")
             return np.array(result_list) if result_list else None
        
         response = requests.post(
            f"http://{device}:{self.default_port}{endpoint}",
            json={"func": serialized_func, "inputs": inputs}
        )
         response.raise_for_status()
         return response.json().get("result")

    def _check_flask_server(self, ip):
        try:
            response = requests.get(f"http://{ip}:{self.default_port}/ram")
            return response.status_code == 200
        except requests.RequestException:
            return False

    def _check_ram(self, ip):
        try:
            response = requests.get(f"http://{ip}:5000/ram", timeout=2)
            if response.status_code == 200:
                result = response.json()  # Expected output: {'total_ram': ..., 'available_ram': ...}
                return result['available_ram']
        except requests.exceptions.RequestException as e:
            pass
        return 0

    def _serialize_function(self, func):
        return cloudpickle.dumps(func).hex()


    def _update_cache(self, key, value):
        with self.cache_lock:
            self.shared_cache[key] = value

    def get_cached_value(self, key):
        with self.cache_lock:
            return self.shared_cache.get(key)

   Example usage:
   # Complete parallelism -> Calculating the sum of squares

import ram_sharing as RS

ram_sharing = RS.RAMSharing()

devices = ram_sharing.get_available_devices(
    ram_min=1, 
    check_server=True,
    max_devices=10
    )

print(devices)

# Define a sample function to execute
def calculate_square_sum(numbers):
    return sum(x**2 for x in numbers)

# Run the distributed function
results = ram_sharing.run_distributed(
    func=calculate_square_sum,
    inputs=[i for i in range(1, 10001)],
    devices=devices,
    partial_parallel=False
)

print("Final result:", sum(results))

# Partial parallelism -> Matrix multiplication

import numpy as np
import ram_sharing as RS

ram_sharing = RS.RAMSharing()

# Get available devices
devices = ram_sharing.get_available_devices(
    ram_min=1, 
    check_server=True,
    max_devices=10
    )

print("Available devices:", devices)

def matrix_multiply(mat):
    mat1 = mat[0]
    mat2 = mat[1]
    return np.dot(mat1, mat2)

# Define two matrices to multiply
matrix_a = np.array([
    [1, 2, 3, 4],
    [5, 6, 7, 8],
    [9, 10, 11, 12],
    [13, 14, 15, 16]
])
matrix_b = np.array([
    [17, 18, 19, 20],
    [21, 22, 23, 24],
    [25, 26, 27, 28],
    [29, 30, 31, 32]
])

inputs = [matrix_a, matrix_b]

# Run the distributed matrix multiplication
result = ram_sharing.run_distributed(
    func=matrix_multiply, 
    inputs=inputs, 
    devices=devices, 
    np_matrix=True, 
    partial_parallel=True
    )

# Print the resultant matrix
print("Resultant Matrix after distributed multiplication:\n", result)


"""

    # prompt 
    prompt = request.json.get('prompt')

    prompt += "\n Don't write multi line comments."
    
    # Prepare full prompt by appending context if provided
    full_prompt = prompt if context is None else f"{context}\n\n{prompt}"
    
    # Generate the response
    response = model.generate_content(full_prompt)

    response = f"""{response.text}"""
    
    # Return the generated text
    return jsonify({"response": response})
